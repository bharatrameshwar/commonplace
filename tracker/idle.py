import subprocess
import re


def get_idle_seconds() -> float:
    """Get the number of seconds since last user input (keyboard/mouse).

    Uses ioreg to read HIDIdleTime from IOHIDSystem. The value is in nanoseconds.
    Returns seconds as a float. Returns 0.0 on failure.
    """
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', result.stdout)
        if match:
            nanoseconds = int(match.group(1))
            return nanoseconds / 1_000_000_000
    except (subprocess.TimeoutExpired, OSError):
        pass
    return 0.0


def is_idle(threshold_seconds: float = 120.0) -> bool:
    """Check if the user has been idle for longer than the threshold."""
    return get_idle_seconds() >= threshold_seconds
