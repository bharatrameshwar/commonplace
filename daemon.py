#!/usr/bin/env python3
"""Activity Tracker capture daemon.

Usage:
    python daemon.py              # Run in foreground
    python daemon.py --interval 5 # Custom poll interval
"""

import argparse
import logging
import os

from tracker.capture import run_capture_loop
from tracker.db import DEFAULT_DB_PATH


def main():
    parser = argparse.ArgumentParser(description="Activity Tracker capture daemon")
    parser.add_argument(
        "--interval", type=float, default=5.0,
        help="Seconds between captures (default: 5)",
    )
    parser.add_argument(
        "--idle-threshold", type=float, default=120.0,
        help="Seconds of inactivity before marking idle (default: 120)",
    )
    parser.add_argument(
        "--screenshot-interval", type=float, default=30.0,
        help="Seconds between screenshots (default: 30)",
    )
    parser.add_argument(
        "--db", type=str, default=DEFAULT_DB_PATH,
        help=f"Database path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    db_path = os.path.expanduser(args.db)
    logging.info("Starting activity tracker daemon")
    logging.info("Database: %s", db_path)

    run_capture_loop(
        interval=args.interval,
        idle_threshold=args.idle_threshold,
        screenshot_interval=args.screenshot_interval,
        db_path=db_path,
    )


if __name__ == "__main__":
    main()
