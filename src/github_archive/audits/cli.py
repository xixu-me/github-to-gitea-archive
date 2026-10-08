"""Dispatch independent audits without importing their runtime until selected."""

import argparse
import runpy
import sys


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", choices=("code", "metadata", "children", "projection", "restore"))
    options, remaining = parser.parse_known_args()
    original = sys.argv[:]
    sys.argv = [original[0] + " " + options.audit, *remaining]
    try:
        runpy.run_module("github_archive.audits." + options.audit, run_name="__main__")
        return 0
    finally:
        sys.argv = original
