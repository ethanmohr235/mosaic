#!/usr/bin/env python3
"""Monolith -- the table view of a MonOSS garden."""
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monolith.app import launch


def main() -> None:
    parser = argparse.ArgumentParser(description="Monolith -- edit a MonOSS garden as a table")
    parser.add_argument("--garden", help="Garden to open (defaults to 'default')")
    parser.add_argument("--view", help="Mote id of a saved view to open on")
    args = parser.parse_args()
    launch(args.garden, args.view)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
