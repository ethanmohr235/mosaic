#!/usr/bin/env python3
"""Gardens -- manage the gardens every other MonOSS window opens."""
import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gardens_app.app import launch


def main() -> None:
    parser = argparse.ArgumentParser(description="Gardens -- create, import and sync MonOSS gardens")
    # Not "the garden this window is open on" -- this window is open on
    # all of them. It is which row to start with selected, so an app
    # opening this window can hand over the garden the user was just
    # looking at.
    parser.add_argument("--garden", help="Garden to start with selected")
    parser.add_argument(
        "--sync", action="store_true", help="Open on that garden's sync settings"
    )
    args = parser.parse_args()
    launch(args.garden, args.sync)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
