import subprocess
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_browser_tab(app_name: str) -> tuple[Optional[str], Optional[str]]:
    """Get the active tab URL and title for a browser app.

    Returns (url, title) tuple. Returns (None, None) if not a supported browser
    or if the query fails.
    """
    generators = {
        "Google Chrome": _chrome_script,
        "Safari": _safari_script,
    }
    # Chromium-based browsers share the same AppleScript API
    chromium_browsers = {"Microsoft Edge", "Arc", "Brave Browser", "Comet", "Dia"}

    if app_name in generators:
        script = generators[app_name]()
    elif app_name in chromium_browsers:
        script = _chromium_script(app_name)
    else:
        return None, None

    return _run_applescript(script)


def _chrome_script() -> str:
    return '''
    tell application "Google Chrome"
        if (count of windows) > 0 then
            set tabURL to URL of active tab of front window
            set tabTitle to title of active tab of front window
            return tabURL & "|||" & tabTitle
        end if
    end tell
    '''


def _chromium_script(app_name: str) -> str:
    return f'''
    tell application "{app_name}"
        if (count of windows) > 0 then
            set tabURL to URL of active tab of front window
            set tabTitle to title of active tab of front window
            return tabURL & "|||" & tabTitle
        end if
    end tell
    '''


def _safari_script() -> str:
    return '''
    tell application "Safari"
        if (count of windows) > 0 then
            set tabURL to URL of front document
            set tabTitle to name of front document
            return tabURL & "|||" & tabTitle
        end if
    end tell
    '''


def _run_applescript(script: str) -> tuple[Optional[str], Optional[str]]:
    """Run an AppleScript and parse URL|||Title output."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and "|||" in result.stdout:
            parts = result.stdout.strip().split("|||", 1)
            url = parts[0].strip() or None
            title = parts[1].strip() or None
            return url, title
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None, None
