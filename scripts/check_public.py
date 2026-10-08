#!/usr/bin/env python3
"""Scan tracked publication files without printing matched credential values."""

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "GitHub token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def main():
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
    )
    paths = [ROOT / name.decode() for name in result.stdout.split(b"\0") if name]
    failures = []
    for path in paths:
        if path.name == Path(__file__).name:
            continue  # The deny-list itself necessarily names forbidden examples.
        if (
            path.name in ("app.json", "webhook-secret")
            or path.suffix in (".pem", ".key", ".db", ".tgz")
            or path.name.startswith(".env")
        ):
            failures.append((path.relative_to(ROOT), "private runtime file"))
        for label, pattern in PATTERNS.items():
            if pattern.search(path.read_bytes()):
                failures.append((path.relative_to(ROOT), label))
    for path, label in failures:
        print(f"BLOCKED: {path}: {label}", file=sys.stderr)
    if failures:
        return 1
    print(
        f"Publication scan passed for {len(paths)} tracked files; runtime credentials/data are excluded."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
