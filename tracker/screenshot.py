import subprocess
import os
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SCREENSHOT_DIR = os.path.expanduser("~/.local/share/commonplace/screenshots")


def take_screenshot(
    screenshot_dir: str = DEFAULT_SCREENSHOT_DIR,
    timestamp: datetime | None = None,
) -> str | None:
    """Capture a screenshot of the current screen.

    Uses macOS `screencapture` command. Saves as compressed JPEG to keep size down.
    Organizes into date-based subdirectories.

    Returns the file path on success, None on failure.
    """
    ts = timestamp or datetime.now()
    date_dir = os.path.join(screenshot_dir, ts.strftime("%Y-%m-%d"))
    Path(date_dir).mkdir(parents=True, exist_ok=True)

    filename = ts.strftime("%H-%M-%S") + ".jpg"
    filepath = os.path.join(date_dir, filename)

    try:
        result = subprocess.run(
            [
                "screencapture",
                "-x",           # no sound
                "-t", "jpg",    # JPEG format
                "-D", "1",      # main display only
                filepath,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and os.path.exists(filepath):
            # Resize to 1920px wide and compress to 55% quality (~400-600KB per image)
            subprocess.run(
                ["sips", "--resampleWidth", "1920", "-s", "formatOptions", "55", filepath, "--out", filepath],
                capture_output=True,
                timeout=10,
            )
            size_kb = os.path.getsize(filepath) / 1024
            logger.debug("Screenshot saved: %s (%.0fKB)", filepath, size_kb)
            return filepath
        else:
            logger.warning("screencapture failed: %s", result.stderr.strip())
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Screenshot failed: %s", e)

    return None
