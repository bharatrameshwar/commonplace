"""Background web page content capture.

When the user browses to a new URL, fetches the page text (stripped of
ads/nav/scripts) and stores it in the database for full-text search.
Runs in a daemon thread to avoid blocking the capture loop.
"""

import logging
import queue
import re
import threading
from typing import Optional
from urllib.parse import urlparse

from tracker.db import insert_web_page, was_recently_fetched

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 50_000  # 50KB text cap per page

# URL patterns to skip
_SKIP_SCHEMES = {"chrome", "chrome-extension", "about", "arc", "edge", "brave", "file", "data"}
_SKIP_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]"}
_SKIP_EXTENSIONS = {".pdf", ".zip", ".exe", ".dmg", ".pkg", ".png", ".jpg", ".jpeg",
                    ".gif", ".svg", ".mp4", ".mp3", ".wav", ".webm", ".webp", ".ico"}


def should_fetch_url(url: str) -> bool:
    """Check if a URL is worth fetching for content capture."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme in _SKIP_SCHEMES:
        return False
    if parsed.hostname in _SKIP_HOSTS:
        return False

    path_lower = parsed.path.lower()
    for ext in _SKIP_EXTENSIONS:
        if path_lower.endswith(ext):
            return False

    # Skip very short URLs (probably not real pages)
    if len(url) < 10:
        return False

    return True


def _fetch_and_extract(url: str) -> Optional[str]:
    """Fetch a URL and extract clean text content using trafilatura."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False,
                                   include_tables=True, favor_recall=True)
        if text and len(text) > 50:  # skip near-empty extractions
            return text[:MAX_CONTENT_LENGTH]
    except Exception as e:
        logger.debug("Failed to extract content from %s: %s", url, e)
    return None


def _worker(q: queue.Queue, db_path: str) -> None:
    """Background worker that processes URL fetch requests."""
    while True:
        try:
            url, title, observation_id = q.get(timeout=30)
        except queue.Empty:
            continue

        try:
            if was_recently_fetched(url, hours=24, db_path=db_path):
                continue

            content = _fetch_and_extract(url)
            if content:
                insert_web_page(url, title, content, observation_id, db_path)
                logger.debug("Captured web content: %s (%d chars)", url[:80], len(content))
        except Exception:
            logger.exception("Error processing URL: %s", url[:80])
        finally:
            q.task_done()


def start_web_content_worker(db_path: str) -> queue.Queue:
    """Start the background web content capture worker.

    Returns a queue — enqueue (url, title, observation_id) tuples.
    """
    q = queue.Queue(maxsize=100)
    t = threading.Thread(target=_worker, args=(q, db_path), daemon=True, name="web-content")
    t.start()
    logger.info("Web content capture worker started")
    return q
