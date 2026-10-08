#!/usr/bin/env python3
"""Install files and render systemd units. Does not start services or alter Gitea."""

import argparse
import json
import grp
import os
from pathlib import Path
import pwd
import shutil
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from archive_config import load_config, read_env_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path("/"), help="stage into this root for review/testing"
    )
    parser.add_argument("--env-file", type=Path, default=Path("/etc/gitea/github-archive.env"))
    parser.add_argument("--user", default="git", help="existing system user that runs Gitea")
    options = parser.parse_args()
    root = options.root.resolve()
    if not options.env_file.is_absolute():
        parser.error("--env-file must be absolute")
    real_install = root == Path("/")
    if real_install and os.geteuid() != 0:
        parser.error("system installation requires root; use --root for staging")
    user = pwd.getpwnam(options.user) if real_install else None

    def target(path):
        return root / str(path).lstrip("/")

    def copy(source, destination, mode):
        destination = target(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(mode)
        return destination

    env_path = target(options.env_file)
    if not env_path.exists():
        env_path = copy(SOURCE / "deploy/archive.env.example", options.env_file, 0o640)
    config = load_config(read_env_file(env_path))
    config.validate()
    env_path.chmod(0o640)
    if user:
        os.chown(env_path, 0, user.pw_gid)
    state = target(config.root)
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    if user:
        os.chown(state, user.pw_uid, user.pw_gid)
    program = Path("/usr/local/lib/github-archive")
    for path in SOURCE.glob("*.py"):
        copy(
            path,
            program / path.name,
            0o755 if path.name not in ("archive_config.py", "audit_common.py") else 0o644,
        )
    for path in (SOURCE / "deploy/systemd").glob("*"):
        text = path.read_text()
        text = text.replace("User=git", "User=" + options.user).replace(
            "Group=git", "Group=" + (grp.getgrgid(user.pw_gid).gr_name if user else options.user)
        )
        text = text.replace(
            "EnvironmentFile=/etc/gitea/github-archive.env",
            "EnvironmentFile="
            + json.dumps(str(options.env_file))
            + "\nEnvironment="
            + json.dumps("ARCHIVE_ENV_FILE=" + str(options.env_file)),
        )
        writable = [config.root]
        if "worker.service" in path.name or "ingest.service" in path.name:
            writable += [config.repo_root, config.gitea_db.parent]
        for old in (
            "ReadWritePaths=/var/lib/github-archive /var/lib/gitea",
            "ReadWritePaths=/var/lib/github-archive",
        ):
            if old in text:
                text = text.replace(
                    old, "ReadWritePaths=" + " ".join(json.dumps(str(p)) for p in writable)
                )
                break
        destination = target(Path("/etc/systemd/system") / path.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
        destination.chmod(0o644)
    nginx = (
        (SOURCE / "deploy/nginx-locations.conf")
        .read_text()
        .replace("127.0.0.1:3091", "127.0.0.1:" + str(config.port))
    )
    destination = target("/etc/nginx/snippets/github-archive.conf")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(nginx)
    destination.chmod(0o644)
    print(
        "Installed runtime, systemd units and Nginx snippet; existing environment file preserved."
    )
    print(
        "Configure credentials and Gitea, include the Nginx snippet, then follow docs/deployment.md."
    )


if __name__ == "__main__":
    main()
