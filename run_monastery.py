#!/usr/bin/env python3
"""Monastery -- the calendar window for a MonOSS garden."""
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monastery.app import launch


def main() -> None:
    parser = argparse.ArgumentParser(description="Monastery -- see a garden's dated motes on a calendar")
    parser.add_argument("--garden", help="Garden to open (defaults to 'default')")
    parser.add_argument("--focus", help="Mote id to open the detail panel on")
    args = parser.parse_args()
    launch(args.garden, args.focus)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
