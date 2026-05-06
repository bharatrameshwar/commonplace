"""People detection and linking from activity data.

Extracts person names from Teams chat titles, Outlook email subjects,
meeting window titles, and classified span descriptions, then links
them to activity spans in the database.
"""

import re
import logging
from typing import Optional

from tracker.config import person_stopwords, self_aliases
from tracker.db import get_or_create_person, insert_person_activity

logger = logging.getLogger(__name__)

# Built-in stopwords: words that look like names but aren't, generic across
# all users. User-specific stopwords (org/product/project names) live in
# config.yaml under `user_profile.person_stopwords` and are merged at runtime.
_BUILTIN_NON_NAMES = {
    # Apps & categories
    "microsoft", "teams", "outlook", "chrome", "safari", "slack", "zoom",
    "google", "meet", "finder", "terminal", "code", "cursor", "claude",
    "cowork", "ticktick", "heptabase", "granola", "telegram", "discord",
    "spotify", "youtube", "notion", "figma", "canva", "webex", "arc",
    # Common window title words
    "chat", "meeting", "call", "mail", "inbox", "sent", "draft", "calendar",
    "new", "reply", "forward", "general", "channel", "private", "group",
    "document", "presentation", "spreadsheet", "file", "folder", "settings",
    "home", "search", "browse", "download", "upload", "share", "edit",
    # Months, days
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    # Common non-name patterns
    "the", "and", "for", "with", "from", "about", "status", "update",
    "review", "sync", "standup", "planning", "retrospective", "sprint",
    "service", "request", "ticket", "issue", "bug", "feature",
    "touch", "point", "data", "platform", "team", "session",
}


def _non_names() -> set[str]:
    """Built-in stopwords plus the user's configured ones."""
    return _BUILTIN_NON_NAMES | person_stopwords()


# Minimum name length
_MIN_NAME_LEN = 3


def normalize_name(name: str) -> str:
    """Normalize a name to canonical form for deduplication."""
    name = name.strip()
    # Handle "Last, First" format
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2 and parts[1]:
            name = f"{parts[1]} {parts[0]}"
    # Strip titles
    for title in ("Mr.", "Mrs.", "Ms.", "Dr.", "Prof."):
        if name.startswith(title + " "):
            name = name[len(title) + 1:]
    return name.strip().lower()


def _is_likely_name(text: str) -> bool:
    """Check if text looks like a person's name (2+ capitalized words)."""
    if len(text) < _MIN_NAME_LEN:
        return False
    words = text.split()
    if len(words) < 2 or len(words) > 4:
        return False
    # All words should start with uppercase
    if not all(w[0].isupper() for w in words):
        return False
    # No word should be a known non-name
    if any(w.lower() in _non_names() for w in words):
        return False
    # At least one word should be >2 chars (not just initials)
    if not any(len(w) > 2 for w in words):
        return False
    return True


def extract_names_from_window(app_name: str, window_title: str) -> list[tuple[str, str]]:
    """Extract person names and interaction types from a window title.

    Returns list of (name, interaction_type) tuples.
    """
    if not window_title:
        return []

    results = []

    # Teams chat: "Chat | Last, First | Microsoft Teams" or
    #             "Chat | Last1, First1; Last2, First2 | Microsoft Teams"
    #             "Title | Microsoft Teams"
    if app_name in ("Microsoft Teams", "MSTeams"):
        # Extract the middle section: "Chat | NAMES | Microsoft Teams"
        match = re.match(r'(?:Chat\s*\|\s*)?(.+?)\s*\|\s*Microsoft Teams', window_title)
        if not match:
            # Fallback: "Chat | NAMES" without trailing "| Microsoft Teams"
            match = re.match(r'Chat\s*\|\s*(.+)', window_title)

        if match:
            names_str = match.group(1).strip()
            # Only extract if it looks like a "Last, First" name pattern
            # Skip: channel names, group chats, meeting titles
            skip_signals = ("#", "[", "General", "team", "meeting", "call",
                            "catchup", "sync", "standup", "issues", "channel",
                            "rockstar", "forecasting", "empowerment", "adoption",
                            "review", "planning", "update", "session", "workshop")
            names_lower = names_str.lower()
            if any(s in names_lower for s in skip_signals):
                pass
            elif "," not in names_str and ";" not in names_str:
                # No comma = not "Last, First" format = probably a group/channel name
                pass
            else:
                for name in _split_teams_names(names_str):
                    results.append((name, "teams_chat"))

    # Outlook: "Mail - Last, First - Subject" or "Inbox - Last, First - Subject"
    elif "outlook" in app_name.lower() or "mail" in window_title.lower():
        parts = window_title.split(" - ")
        if len(parts) >= 2:
            # Name is usually in position 1 (after "Mail"/"Inbox")
            name_part = parts[1].strip() if parts[0].strip().lower() in ("mail", "inbox", "reading pane") else None
            if name_part and "," in name_part:
                # "Last, First" format
                lf = name_part.split(",", 1)
                potential_name = f"{lf[1].strip()} {lf[0].strip()}"
            elif name_part:
                potential_name = name_part
            else:
                potential_name = None
            if potential_name and _is_likely_name(potential_name):
                results.append((potential_name, "email"))

    # Zoom/Meet: meeting participant names
    elif app_name in ("zoom.us", "Google Meet"):
        for name in _extract_names_from_text(window_title):
            results.append((name, "meeting"))

    return results


def extract_names_from_description(description: str) -> list[str]:
    """Extract person names from a classified span description."""
    if not description:
        return []
    return _extract_names_from_text(description)


def _split_teams_names(text: str) -> list[str]:
    """Split Teams "Last, First" or "Last1, First1; Last2, First2" name strings.

    Many enterprise Teams tenants render names in "Last, First" format.
    Multiple people are separated by semicolons or by the pattern
    "Last1, First1, Last2, First2" (alternating last/first).
    """
    names = []

    # If semicolons present, split on those — each part is "Last, First"
    if ";" in text:
        for part in text.split(";"):
            part = part.strip()
            if "," in part:
                last, first = part.split(",", 1)
                name = f"{first.strip()} {last.strip()}"
                if _is_likely_name(name):
                    names.append(name)
        return names

    # Handle pipe-separated name section that might contain "Last, First" pairs
    # Pattern: pairs of words separated by commas, where each pair is Last, First
    parts = [p.strip() for p in text.split(",")]

    if len(parts) == 2:
        # Single person: "Last, First"
        name = f"{parts[1]} {parts[0]}"
        if _is_likely_name(name):
            names.append(name)
    elif len(parts) % 2 == 0 and len(parts) > 2:
        # Multiple people: "Last1, First1, Last2, First2, ..."
        for i in range(0, len(parts), 2):
            name = f"{parts[i+1]} {parts[i]}"
            if _is_likely_name(name):
                names.append(name)
    else:
        # Odd number of comma-separated items — might be "First Last, First Last" format
        for part in parts:
            part = part.strip()
            if _is_likely_name(part):
                names.append(part)

    return names


def _split_names(text: str) -> list[str]:
    """Split a comma-separated or 'and'-separated name list."""
    # Replace " and " with comma
    text = re.sub(r'\s+and\s+', ', ', text)
    return [n.strip() for n in text.split(",") if n.strip()]


def _extract_names_from_text(text: str) -> list[str]:
    """Find sequences of 2-3 capitalized words that look like names."""
    names = []
    # Match "Firstname Lastname" patterns (2-3 consecutive capitalized words)
    for match in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b', text):
        candidate = match.group(1)
        if _is_likely_name(candidate):
            names.append(candidate)
    return names


def extract_and_link_people(
    span_id: int,
    app_name: str,
    window_title: Optional[str],
    description: Optional[str],
    timestamp: str,
    db_path: str,
) -> list[int]:
    """Extract people from span metadata and link them to the span.

    Returns list of person IDs that were linked.
    """
    seen = set()
    person_ids = []

    # Extract from window title
    for name, interaction_type in extract_names_from_window(app_name, window_title or ""):
        canonical = normalize_name(name)
        if canonical in self_aliases() or canonical in seen or len(canonical) < _MIN_NAME_LEN:
            continue
        seen.add(canonical)
        pid = get_or_create_person(name, canonical, timestamp, db_path)
        insert_person_activity(pid, interaction_type, window_title or "", timestamp,
                               span_id=span_id, db_path=db_path)
        person_ids.append(pid)

    return person_ids
