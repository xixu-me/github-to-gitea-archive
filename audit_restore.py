#!/usr/bin/env python3
"""Independently restore both local snapshot generations without changing live data."""

import contextlib
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import time

from audit_common import load_archive


def main():
    a = load_archive()
    import argparse

    argparse.ArgumentParser(description=__doc__).parse_args()
    ROOT = a.ROOT
    report = {"checked": int(time.time()), "generations": []}
    for generation in ("current", "previous"):
        item = {"generation": generation, "databases": [], "missing_targets": []}
        with tempfile.TemporaryDirectory(prefix="restore-audit-", dir=ROOT) as directory:
            directory = Path(directory)
            names = ["archive", "gitea"]
            if a.snapshot_path("inbox", generation).exists():
                names.append("inbox")
            for name in names:
                source = ROOT / "snapshots" / f"{name}.{generation}.db.gz"
                target = directory / f"{name}.db"
                with gzip.open(source, "rb") as src, target.open("wb") as dst:
                    while block := src.read(1024**2):
                        if shutil.disk_usage(ROOT).free - len(block) < a.LOW:
                            raise RuntimeError("restore audit stopped: capacity reserve")
                        dst.write(block)
                with contextlib.closing(
                    sqlite3.connect(f"file:{target}?mode=ro&immutable=1", uri=True)
                ) as db:
                    integrity = db.execute("PRAGMA integrity_check").fetchall()
                    if integrity != [("ok",)]:
                        raise RuntimeError("restored snapshot integrity failed")
                with source.open("rb") as compressed:
                    compressed_digest = hashlib.file_digest(compressed, "sha256").hexdigest()
                item["databases"].append(
                    {
                        "name": name,
                        "bytes": target.stat().st_size,
                        "compressed_sha256": compressed_digest,
                        "integrity": "ok",
                    }
                )
            with (
                contextlib.closing(
                    sqlite3.connect(
                        f"file:{directory / 'archive.db'}?mode=ro&immutable=1", uri=True
                    )
                ) as c,
                contextlib.closing(
                    sqlite3.connect(f"file:{directory / 'gitea.db'}?mode=ro&immutable=1", uri=True)
                ) as g,
            ):
                count = 0
                for rid, kind, sid, tid, fingerprint in c.execute("SELECT * FROM mapping"):
                    name = c.execute(
                        "SELECT coalesce(local_name,name) FROM repo WHERE id=?", (rid,)
                    ).fetchone()[0]
                    repo = g.execute(
                        "SELECT r.id FROM repository r JOIN user u ON u.id=r.owner_id WHERE u.lower_name=? AND r.lower_name=?",
                        (a.GITEA_OWNER.lower(), name.lower()),
                    ).fetchone()
                    native = None
                    if repo:
                        if kind in ("issue", "pull"):
                            native = g.execute(
                                'SELECT id FROM issue WHERE repo_id=? AND "index"=? AND is_pull=?',
                                (repo[0], tid, kind == "pull"),
                            ).fetchone()
                        elif kind == "comment":
                            native = g.execute(
                                "SELECT x.id FROM comment x JOIN issue i ON i.id=x.issue_id WHERE i.repo_id=? AND x.id=?",
                                (repo[0], tid),
                            ).fetchone()
                        elif kind in ("label", "milestone", "release"):
                            native = g.execute(
                                f"SELECT id FROM {kind} WHERE repo_id=? AND id=?", (repo[0], tid)
                            ).fetchone()
                        else:
                            raise RuntimeError("unexpected mapping kind")
                    count += 1
                    if not native:
                        item["missing_targets"].append(
                            {"repo_id": rid, "kind": kind, "source_id": sid, "target": tid}
                        )
                item["checked_mappings"] = count
        config = ROOT / "snapshots" / f"configuration.{generation}.tgz"
        with tarfile.open(config, "r:gz") as archive:
            names = []
            for member in archive.getmembers():
                if not member.isfile() or member.size > 1024**2:
                    raise RuntimeError("unexpected configuration member")
                archive.extractfile(member).read()
                names.append(member.name)
        item["configuration_members"] = len(names)
        item["configuration_valid"] = True
        item["ok"] = not item["missing_targets"]
        report["generations"].append(item)
    report["ok"] = all(item["ok"] for item in report["generations"])
    target = ROOT / "audit-restore.json"
    temp = target.with_suffix(".next.json")
    temp.write_text(json.dumps(report))
    temp.chmod(0o600)
    temp.replace(target)
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
