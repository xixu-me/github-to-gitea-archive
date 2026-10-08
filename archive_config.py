"""Environment configuration for one isolated GitHub account archive."""

import os
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import urllib.parse


@dataclass(frozen=True)
class Config:
    owner: str
    gitea_owner: str
    admin_user: str
    root: Path
    gitea_url: str
    public_url: str
    repo_root: Path
    gitea_db: Path
    custom_dir: Path
    port: int
    minimum_free: int
    normal_free: int
    snapshot_budget: int
    account_type: str
    app_name: str
    snapshot_paths: tuple

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", self.owner):
            raise ValueError("GITHUB_OWNER must be a GitHub user or organization login")
        for name, value in [
            ("GITEA_OWNER", self.gitea_owner),
            ("ARCHIVE_ADMIN_USER", self.admin_user),
        ]:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value):
                raise ValueError(name + " must be a Gitea login without path separators")
        url = urllib.parse.urlsplit(self.gitea_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("GITEA_URL must be an HTTP(S) base URL without credentials")
        if self.public_url:
            url = urllib.parse.urlsplit(self.public_url)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in ("", "/")
            ):
                raise ValueError("ARCHIVE_PUBLIC_URL must be an HTTPS origin without a subpath")
        for name, path in [
            ("ARCHIVE_ROOT", self.root),
            ("GITEA_REPO_ROOT", self.repo_root),
            ("GITEA_DB_PATH", self.gitea_db),
            ("GITEA_CUSTOM_DIR", self.custom_dir),
        ]:
            if (
                not path.is_absolute()
                or ".." in path.parts
                or any(c in str(path) for c in "\n\r\x00")
            ):
                raise ValueError(name + " must be an absolute path without parent traversal")
        for path in self.snapshot_paths:
            if (
                not path.is_absolute()
                or ".." in path.parts
                or any(c in str(path) for c in "\n\r\x00")
            ):
                raise ValueError(
                    "ARCHIVE_SNAPSHOT_PATHS must contain absolute paths without parent traversal"
                )
        if not 1 <= self.port <= 65535:
            raise ValueError("ARCHIVE_PORT must be between 1 and 65535")
        if (
            self.minimum_free <= 0
            or self.normal_free < self.minimum_free
            or self.snapshot_budget <= 0
        ):
            raise ValueError("disk budgets must be positive; normal free space must exceed minimum")
        if self.account_type not in ("auto", "user", "organization"):
            raise ValueError("GITHUB_ACCOUNT_TYPE must be auto, user or organization")
        if not self.app_name or len(self.app_name) > 34:
            raise ValueError("GITHUB_APP_NAME must contain 1 to 34 characters")


def load_config(environ=None):
    env = os.environ if environ is None else environ
    owner = env.get("GITHUB_OWNER", "")
    gitea_owner = env.get("GITEA_OWNER", owner)
    return Config(
        owner=owner,
        gitea_owner=gitea_owner,
        admin_user=env.get("ARCHIVE_ADMIN_USER", gitea_owner),
        root=Path(env.get("ARCHIVE_ROOT", "/var/lib/github-archive")),
        gitea_url=env.get("GITEA_URL", "http://127.0.0.1:3000").rstrip("/"),
        public_url=env.get("ARCHIVE_PUBLIC_URL", "").rstrip("/"),
        repo_root=Path(env.get("GITEA_REPO_ROOT", "/var/lib/gitea/repositories")),
        gitea_db=Path(env.get("GITEA_DB_PATH", "/var/lib/gitea/data/gitea.db")),
        custom_dir=Path(env.get("GITEA_CUSTOM_DIR", "/var/lib/gitea/custom")),
        port=int(env.get("ARCHIVE_PORT", "3091")),
        minimum_free=int(env.get("ARCHIVE_MIN_FREE_MIB", "500")) * 1024**2,
        normal_free=int(env.get("ARCHIVE_NORMAL_FREE_MIB", "1536")) * 1024**2,
        snapshot_budget=int(env.get("ARCHIVE_SNAPSHOT_BUDGET_MIB", "512")) * 1024**2,
        account_type=env.get("GITHUB_ACCOUNT_TYPE", "auto"),
        app_name=env.get("GITHUB_APP_NAME", (owner[:18] + " GitHub Archive").strip()),
        snapshot_paths=tuple(
            Path(p) for p in env.get("ARCHIVE_SNAPSHOT_PATHS", "").split(":") if p
        ),
    )


def read_env_file(path):
    """Read literal KEY=value assignments; never execute shell code or expansions."""
    result = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment assignment at line {number}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"invalid environment key at line {number}")
        parts = shlex.split(value, comments=True)
        if len(parts) > 1:
            raise ValueError(f"quote environment values containing spaces at line {number}")
        result[key] = parts[0] if parts else ""
    return result
