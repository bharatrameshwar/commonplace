#!/usr/bin/env python3
"""Run daily storage cleanup for the activity tracker."""

import logging
import os

from tracker.cleanup import run_cleanup
from tracker.db import DEFAULT_DB_PATH
from tracker.people_summary import generate_weekly_summaries
from tracker.screenshot import DEFAULT_SCREENSHOT_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

if __name__ == "__main__":
    db_path = os.path.expanduser(DEFAULT_DB_PATH)
    run_cleanup(
        db_path=db_path,
        screenshot_dir=os.path.expanduser(DEFAULT_SCREENSHOT_DIR),
        keep_screenshots_days=7,
        keep_observations_days=30,
    )
    try:
        stats = generate_weekly_summaries(db_path=db_path)
        logging.info("People weekly summaries: %s", stats)
    except Exception as exc:
        logging.warning("People weekly summaries failed: %s", exc)
