#!/usr/bin/env python3
"""Momentum -- the task board, week planner and timer for a MonOSS garden."""
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from momentum.app import launch


def main() -> None:
    parser = argparse.ArgumentParser(description="Momentum -- plan and track a garden's tasks")
    parser.add_argument("--garden", help="Garden to open (defaults to 'default')")
    parser.add_argument("--focus", help="Mote id to open the detail panel on")
    args = parser.parse_args()
    launch(args.garden, args.focus)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
