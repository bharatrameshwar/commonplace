"""Storage management for activity tracker.

Retention tiers:
  - Last 7 days: keep everything (screenshots + observations + spans)
  - 7-30 days: delete screenshots, keep observations + spans
  - 30+ days: delete observations, keep only classified spans
"""

import os
import logging
import shutil
from datetime import date, timedelta

from tracker.db import DEFAULT_DB_PATH, get_connection, init_db
from tracker.screenshot import DEFAULT_SCREENSHOT_DIR

logger = logging.getLogger(__name__)


def run_cleanup(
    db_path: str = DEFAULT_DB_PATH,
    screenshot_dir: str = DEFAULT_SCREENSHOT_DIR,
    keep_screenshots_days: int = 7,
    keep_observations_days: int = 30,
):
    """Run the full cleanup cycle.

    Args:
        db_path: Path to SQLite database.
        screenshot_dir: Path to screenshots directory.
        keep_screenshots_days: Days to keep screenshots (default 7).
        keep_observations_days: Days to keep raw observations (default 30).
    """
    init_db(db_path)
    today = date.today()

    # Tier 1: Delete screenshots older than keep_screenshots_days
    screenshot_cutoff = today - timedelta(days=keep_screenshots_days)
    deleted_screenshots = _delete_old_screenshots(screenshot_dir, screenshot_cutoff)
    if deleted_screenshots > 0:
        _clear_screenshot_paths(db_path, screenshot_cutoff)
        logger.info("Deleted %d screenshot(s) older than %s", deleted_screenshots, screenshot_cutoff)

    # Tier 2: Delete raw observations older than keep_observations_days
    # (only if they've been classified — never delete unclassified data)
    observation_cutoff = today - timedelta(days=keep_observations_days)
    deleted_obs = _delete_old_observations(db_path, observation_cutoff)
    if deleted_obs > 0:
        logger.info("Deleted %d classified observation(s) older than %s", deleted_obs, observation_cutoff)

    # Tier 3: Delete web page content older than 90 days (keep URL + title)
    web_cutoff = today - timedelta(days=90)
    conn = get_connection(db_path)
    cursor = conn.execute(
        "UPDATE web_pages SET content = NULL, content_length = 0 WHERE captured_at < ? AND content IS NOT NULL",
        (web_cutoff.isoformat(),),
    )
    if cursor.rowcount > 0:
        logger.info("Cleared content from %d web page(s) older than %s", cursor.rowcount, web_cutoff)
    conn.commit()
    conn.close()

    # Vacuum the database to reclaim space
    conn = get_connection(db_path)
    conn.execute("VACUUM")
    conn.close()

    # Report current storage
    _report_storage(db_path, screenshot_dir)


def _delete_old_screenshots(screenshot_dir: str, cutoff: date) -> int:
    """Delete screenshot date directories older than cutoff."""
    deleted = 0
    if not os.path.exists(screenshot_dir):
        return 0

    for dirname in os.listdir(screenshot_dir):
        dirpath = os.path.join(screenshot_dir, dirname)
        if not os.path.isdir(dirpath):
            continue
        try:
            dir_date = date.fromisoformat(dirname)
            if dir_date < cutoff:
                file_count = len(os.listdir(dirpath))
                shutil.rmtree(dirpath)
                deleted += file_count
                logger.debug("Removed screenshot directory: %s (%d files)", dirname, file_count)
        except ValueError:
            continue  # Not a date-formatted directory

    return deleted


def _clear_screenshot_paths(db_path: str, cutoff: date) -> None:
    """Set screenshot_path to NULL for observations older than cutoff."""
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE observations SET screenshot_path = NULL WHERE timestamp < ? AND screenshot_path IS NOT NULL",
        (cutoff.isoformat(),),
    )
    conn.commit()
    conn.close()


def _delete_old_observations(db_path: str, cutoff: date) -> int:
    """Delete classified observations older than cutoff."""
    conn = get_connection(db_path)
    cursor = conn.execute(
        "DELETE FROM observations WHERE timestamp < ? AND classified = 1",
        (cutoff.isoformat(),),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def _report_storage(db_path: str, screenshot_dir: str) -> None:
    """Log current storage usage."""
    # Database size
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    # Screenshots total size
    screenshot_size = 0
    screenshot_count = 0
    if os.path.exists(screenshot_dir):
        for root, dirs, files in os.walk(screenshot_dir):
            for f in files:
                filepath = os.path.join(root, f)
                screenshot_size += os.path.getsize(filepath)
                screenshot_count += 1

    logger.info(
        "Storage: DB=%.1fMB, Screenshots=%d files (%.1fMB), Total=%.1fMB",
        db_size / 1_048_576,
        screenshot_count,
        screenshot_size / 1_048_576,
        (db_size + screenshot_size) / 1_048_576,
    )


def get_storage_stats(
    db_path: str = DEFAULT_DB_PATH,
    screenshot_dir: str = DEFAULT_SCREENSHOT_DIR,
) -> dict:
    """Return storage stats as a dict (for MCP server)."""
    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    screenshot_size = 0
    screenshot_count = 0
    screenshots_by_date = {}
    if os.path.exists(screenshot_dir):
        for dirname in sorted(os.listdir(screenshot_dir)):
            dirpath = os.path.join(screenshot_dir, dirname)
            if not os.path.isdir(dirpath):
                continue
            files = os.listdir(dirpath)
            dir_size = sum(os.path.getsize(os.path.join(dirpath, f)) for f in files)
            screenshot_count += len(files)
            screenshot_size += dir_size
            screenshots_by_date[dirname] = {
                "files": len(files),
                "size_mb": round(dir_size / 1_048_576, 1),
            }

    conn = get_connection(db_path)
    total_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    total_spans = conn.execute("SELECT COUNT(*) FROM activity_spans").fetchone()[0]
    unclassified = conn.execute("SELECT COUNT(*) FROM observations WHERE classified = 0").fetchone()[0]
    with_screenshots = conn.execute("SELECT COUNT(*) FROM observations WHERE screenshot_path IS NOT NULL").fetchone()[0]
    # Web pages and people stats (handle missing tables gracefully)
    try:
        total_web_pages = conn.execute("SELECT COUNT(*) FROM web_pages").fetchone()[0]
        total_people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    except Exception:
        total_web_pages = 0
        total_people = 0
    conn.close()

    return {
        "database_size_mb": round(db_size / 1_048_576, 1),
        "screenshots_total_mb": round(screenshot_size / 1_048_576, 1),
        "total_size_mb": round((db_size + screenshot_size) / 1_048_576, 1),
        "screenshot_count": screenshot_count,
        "observations_with_screenshots": with_screenshots,
        "total_observations": total_obs,
        "unclassified_observations": unclassified,
        "total_spans": total_spans,
        "total_web_pages": total_web_pages,
        "total_people": total_people,
        "screenshots_by_date": screenshots_by_date,
    }
