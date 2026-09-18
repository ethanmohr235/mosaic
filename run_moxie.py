#!/usr/bin/env python3
"""Moxie -- the graphing window of a MonOSS garden."""
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from moxie.app import launch


def main() -> None:
    parser = argparse.ArgumentParser(description="Moxie -- graph a MonOSS garden")
    parser.add_argument("--garden", help="Garden to open (defaults to 'default')")
    parser.add_argument("--focus", help="Mote id to open with selected")
    args = parser.parse_args()
    launch(args.garden, args.focus)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
