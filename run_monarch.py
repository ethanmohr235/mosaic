#!/usr/bin/env python3
"""Monarch -- the graph window for a MonOSS garden."""
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monarch.app import launch


def main() -> None:
    parser = argparse.ArgumentParser(description="Monarch -- explore a garden as a graph")
    parser.add_argument("--garden", help="Garden to open (defaults to the last one opened)")
    args = parser.parse_args()
    launch(args.garden)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
