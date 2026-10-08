#!/usr/bin/python3
"""Bound web diff work while keeping clone/fetch/packing configuration intact."""

import os
from pathlib import Path
import sys


def diff_command(args):
    takes_value = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env"}
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in takes_value:
            skip = True
        elif arg.startswith("-"):
            continue
        else:
            return arg in {"diff", "show", "log"}
    return False


def main():
    args = sys.argv[1:]
    config = []
    if diff_command(args):
        config = ["-c", "core.bigFileThreshold=8m", "-c", "diff.renameLimit=100"]
        try:
            Path("/proc/self/oom_score_adj").write_text("500")
        except OSError:
            pass
    os.execv("/usr/bin/git", ["/usr/bin/git"] + config + args)


if __name__ == "__main__":
    main()
