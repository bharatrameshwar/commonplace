import subprocess
import time
import logging
from datetime import datetime
from typing import Optional

from tracker.models import Observation
from tracker.db import insert_observation, init_db, upsert_link
from tracker.idle import is_idle
from tracker.browser import get_browser_tab
from tracker.screenshot import take_screenshot
from tracker.web_content import should_fetch_url, start_web_content_worker

logger = logging.getLogger(__name__)

BROWSERS = {"Google Chrome", "Safari", "Arc", "Microsoft Edge", "Brave Browser", "Comet", "Dia"}
# Meeting apps — never mark as idle when these are frontmost (user is watching/listening)
MEETING_APPS = {"zoom.us", "MSTeams", "Microsoft Teams", "Webex", "FaceTime", "Google Meet"}


def get_frontmost_app() -> Optional[str]:
    """Get the name of the currently frontmost application via AppleScript."""
    script = '''
    tell application "System Events"
        set frontApp to name of first application process whose frontmost is true
        return frontApp
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def get_window_title() -> Optional[str]:
    """Get the window title of the frontmost application."""
    script = '''
    tell application "System Events"
        tell (first application process whose frontmost is true)
            if (count of windows) > 0 then
                return name of front window
            end if
        end tell
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def capture_once(
    idle_threshold: float = 120.0,
    db_path: Optional[str] = None,
    take_screenshot_now: bool = False,
) -> Optional[Observation]:
    """Capture a single observation of the current activity."""
    kwargs = {"db_path": db_path} if db_path else {}

    app_name = get_frontmost_app()
    if not app_name:
        return None

    window_title = get_window_title()
    user_idle = is_idle(idle_threshold)

    # Override idle for meeting apps — user is watching/listening, not AFK
    if user_idle and app_name in MEETING_APPS:
        user_idle = False

    browser_url = None
    browser_tab_title = None
    if app_name in BROWSERS and not user_idle:
        browser_url, browser_tab_title = get_browser_tab(app_name)

    screenshot_path = None
    if take_screenshot_now and not user_idle:
        now = datetime.now()
        screenshot_path = take_screenshot(timestamp=now)

    obs = Observation(
        timestamp=datetime.now(),
        app_name=app_name,
        window_title=window_title,
        browser_url=browser_url,
        browser_tab_title=browser_tab_title,
        is_idle=user_idle,
        screenshot_path=screenshot_path,
    )

    obs.id = insert_observation(obs, **kwargs)

    # Upsert link for every browser URL captured
    if browser_url:
        try:
            context = browser_tab_title or window_title or ""
            upsert_link(
                url=browser_url,
                title=browser_tab_title,
                timestamp=obs.timestamp,
                context=context,
                source=app_name,
                dwell_seconds=kwargs.get("interval", 5.0),
                **({k: v for k, v in kwargs.items() if k == "db_path"}),
            )
        except Exception as e:
            logger.debug("Link upsert failed: %s", e)

    return obs


def run_capture_loop(
    interval: float = 5.0,
    idle_threshold: float = 120.0,
    screenshot_interval: float = 30.0,
    db_path: Optional[str] = None,
    stop_event=None,
):
    """Run the capture loop, polling every `interval` seconds.

    Args:
        interval: Seconds between captures.
        idle_threshold: Seconds of inactivity before marking as idle.
        screenshot_interval: Seconds between screenshots (default 30).
        db_path: Path to SQLite database.
        stop_event: threading.Event to signal stop. If None, runs until KeyboardInterrupt.
    """
    kwargs = {}
    if db_path:
        kwargs["db_path"] = db_path

    init_db(**kwargs)
    logger.info(
        "Capture loop started (interval=%.1fs, screenshot_interval=%.0fs, idle_threshold=%.0fs)",
        interval, screenshot_interval, idle_threshold,
    )

    last_screenshot = 0.0
    last_url = None
    web_queue = start_web_content_worker(db_path or "")

    try:
        while stop_event is None or not stop_event.is_set():
            start = time.monotonic()
            try:
                # Only capture during active hours (8am-9pm)
                current_hour = datetime.now().hour
                if current_hour < 8 or current_hour >= 21:
                    elapsed = time.monotonic() - start
                    sleep_time = max(0, interval - elapsed)
                    if stop_event:
                        stop_event.wait(sleep_time)
                    else:
                        time.sleep(sleep_time)
                    continue

                # Take screenshot if enough time has elapsed
                take_ss = (start - last_screenshot) >= screenshot_interval
                obs = capture_once(idle_threshold, db_path, take_screenshot_now=take_ss)
                if obs:
                    if obs.screenshot_path:
                        last_screenshot = start
                    # Enqueue new URLs for web content capture
                    if obs.browser_url and obs.browser_url != last_url and should_fetch_url(obs.browser_url):
                        try:
                            web_queue.put_nowait((obs.browser_url, obs.browser_tab_title, obs.id))
                        except Exception:
                            pass  # queue full, skip
                    last_url = obs.browser_url
                    status = "idle" if obs.is_idle else obs.app_name
                    ss_flag = " [screenshot]" if obs.screenshot_path else ""
                    logger.debug("Captured: %s | %s%s", status, obs.window_title or "", ss_flag)
            except Exception:
                logger.exception("Error during capture")

            elapsed = time.monotonic() - start
            sleep_time = max(0, interval - elapsed)
            if stop_event:
                stop_event.wait(sleep_time)
            else:
                time.sleep(sleep_time)
    except KeyboardInterrupt:
        logger.info("Capture loop stopped by user")
