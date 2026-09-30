#!/usr/bin/env python3
"""Mosaic -- the console, one tiled window over a MonOSS garden."""
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from console.app import launch


def main() -> None:
    parser = argparse.ArgumentParser(description="Mosaic -- the console over a MonOSS garden")
    parser.add_argument("--garden", help="Garden to open (defaults to your default garden)")
    args = parser.parse_args()
    launch(args.garden)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
