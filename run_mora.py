#!/usr/bin/env python3
"""Mora -- the .modoc note editor for a MonOSS garden."""
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mora.app import launch


def main() -> None:
    parser = argparse.ArgumentParser(description="Mora -- edit .modoc notes on a garden")
    parser.add_argument("--garden", help="Garden to open (defaults to 'default')")
    parser.add_argument("--note", help="Mote id to open the note of")
    args = parser.parse_args()
    launch(args.garden, args.note)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
