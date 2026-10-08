"""Shared environment loading for read-only acceptance commands."""

import argparse
import os
import sys
from ..config import load_config, read_env_file


def load_archive():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--env-file")
    options, remaining = parser.parse_known_args()
    if options.env_file:
        os.environ.update(read_env_file(options.env_file))
        from pathlib import Path

        os.environ["ARCHIVE_ENV_FILE"] = str(Path(options.env_file).resolve())
    sys.argv[1:] = remaining
    from .. import archive

    config = load_config()
    if "--help" not in remaining and "-h" not in remaining:
        config.validate()
    archive.configure(config)
    return archive
