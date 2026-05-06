#!/usr/bin/env python3
"""Activity Tracker MCP Server.

Exposes activity tracking data to Claude via MCP tools.
Claude can read raw observations, classify them, and generate reports.
"""

import base64
import json
import os
from datetime import date as date_mod, datetime
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent

from tracker.db import (
    DEFAULT_DB_PATH,
    add_category,
    get_categories,
    get_category_breakdown,
    get_observations_for_date,
    get_recent_observations,
    get_spans_for_date,
    get_unclassified_spans,
    init_db,
    insert_activity_span,
)
from tracker.models import ActivitySpan

DB_PATH = os.environ.get("ACTIVITY_TRACKER_DB", DEFAULT_DB_PATH)

mcp = FastMCP(
    "Activity Tracker",
    instructions="Track and classify macOS application usage. Captures which app is active, window titles, browser URLs, and lets you classify activities into categories.",
)


@mcp.tool()
def get_recent_activity(minutes: int = 30) -> str:
    """Get raw activity observations from the last N minutes.

    Returns a list of what the user was doing, including app name,
    window title, browser URL (if applicable), and idle status.
    Use this to see what the user has been doing recently.
    """
    init_db(DB_PATH)
    observations = get_recent_observations(minutes, DB_PATH)
    if not observations:
        return f"No activity captured in the last {minutes} minutes. Is the capture daemon running?"

    results = []
    for obs in observations:
        entry = {
            "timestamp": obs.timestamp.strftime("%H:%M:%S"),
            "app": obs.app_name,
            "window": obs.window_title,
            "idle": obs.is_idle,
        }
        if obs.browser_url:
            entry["url"] = obs.browser_url
        if obs.browser_tab_title:
            entry["tab_title"] = obs.browser_tab_title
        if obs.screenshot_path:
            entry["screenshot"] = obs.screenshot_path
        results.append(entry)

    return json.dumps(results, indent=2)


@mcp.tool()
def get_unclassified_activity() -> str:
    """Get activity spans that haven't been classified yet.

    Returns grouped activity spans (consecutive observations of the same app/context
    merged together) with duration, app name, window title, and URLs.

    Use this to see what needs classification, then call classify_spans to categorize them.
    Each span has an observation_ids list — pass those to classify_spans.
    """
    init_db(DB_PATH)
    spans = get_unclassified_spans(DB_PATH)
    if not spans:
        return "No unclassified activity spans. Everything is up to date!"

    results = []
    for i, span in enumerate(spans, 1):
        entry = {
            "index": i,
            "start": span.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "end": span.end_time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_minutes": round(span.duration_seconds / 60, 1),
            "app": span.app_name,
            "window": span.window_title,
            "observation_ids": span.observation_ids,
            "observation_count": span.observation_count,
        }
        if span.browser_url:
            entry["url"] = span.browser_url
        results.append(entry)

    return json.dumps(results, indent=2)


@mcp.tool()
def get_activity_for_date(target_date: str = "") -> str:
    """Get all raw observations for a specific date.

    Args:
        target_date: Date in YYYY-MM-DD format. Defaults to today.

    Returns all captured observations for that date.
    """
    init_db(DB_PATH)
    if target_date:
        d = date_mod.fromisoformat(target_date)
    else:
        d = date_mod.today()

    observations = get_observations_for_date(d, DB_PATH)
    if not observations:
        return f"No activity captured for {d.isoformat()}."

    results = []
    for obs in observations:
        entry = {
            "timestamp": obs.timestamp.strftime("%H:%M:%S"),
            "app": obs.app_name,
            "window": obs.window_title,
            "idle": obs.is_idle,
            "classified": obs.classified,
        }
        if obs.browser_url:
            entry["url"] = obs.browser_url
        results.append(entry)

    return json.dumps(results, indent=2)


@mcp.tool()
def classify_spans(classifications: str) -> str:
    """Classify activity spans by providing categories for each.

    Args:
        classifications: A JSON string containing a list of classifications.
            Each item should have:
            - observation_ids: list of observation IDs (from get_unclassified_activity)
            - category: one of the predefined categories (e.g., "Code/Development", "Email", "Meetings/Calls")
            - subcategory: optional finer-grained category
            - description: a one-line description of what the user was doing

    Example:
        [
            {
                "observation_ids": [1, 2, 3],
                "category": "Code/Development",
                "subcategory": "Code Review",
                "description": "Reviewing PR #452 on GitHub"
            },
            {
                "observation_ids": [4, 5],
                "category": "Communication",
                "subcategory": "Slack",
                "description": "Chatting in #engineering channel"
            }
        ]
    """
    init_db(DB_PATH)
    items = json.loads(classifications)
    created = 0

    for item in items:
        obs_ids = item["observation_ids"]
        if not obs_ids:
            continue

        # Fetch the observations to build the span
        from tracker.db import get_connection

        conn = get_connection(DB_PATH)
        placeholders = ",".join("?" * len(obs_ids))
        rows = conn.execute(
            f"SELECT * FROM observations WHERE id IN ({placeholders}) ORDER BY timestamp",
            obs_ids,
        ).fetchall()
        conn.close()

        if not rows:
            continue

        first_ts = datetime.fromisoformat(rows[0]["timestamp"])
        last_ts = datetime.fromisoformat(rows[-1]["timestamp"])
        duration = max(5, int((last_ts - first_ts).total_seconds()))

        span = ActivitySpan(
            start_time=first_ts,
            end_time=last_ts,
            duration_seconds=duration,
            app_name=rows[0]["app_name"],
            window_title=rows[0]["window_title"],
            browser_url=rows[0]["browser_url"],
            category=item["category"],
            subcategory=item.get("subcategory"),
            description=item.get("description"),
            observation_count=len(rows),
            observation_ids=obs_ids,
        )

        span_id = insert_activity_span(span, DB_PATH)

        # Extract and link people from this span
        try:
            from tracker.people import extract_and_link_people
            extract_and_link_people(
                span_id=span_id,
                app_name=span.app_name,
                window_title=span.window_title,
                description=span.description,
                timestamp=first_ts.isoformat(),
                db_path=DB_PATH,
            )
        except Exception:
            pass  # don't fail classification if people extraction errors

        created += 1

    return f"Successfully classified {created} activity span(s)."


@mcp.tool()
def get_activity_report(target_date: str = "", detailed: bool = False) -> str:
    """Get a time breakdown report showing how time was spent.

    Args:
        target_date: Date in YYYY-MM-DD format. Defaults to today.
        detailed: If true, include individual activity spans.

    Returns a summary of time spent in each category.
    """
    init_db(DB_PATH)
    if target_date:
        d = date_mod.fromisoformat(target_date)
    else:
        d = date_mod.today()

    breakdown = get_category_breakdown(d, DB_PATH)
    if not breakdown:
        return f"No classified activity for {d.isoformat()}. Run classify_spans first."

    total_seconds = sum(row["total_seconds"] for row in breakdown)
    lines = [f"Activity Report for {d.isoformat()}", "=" * 50]

    for row in breakdown:
        seconds = row["total_seconds"]
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        pct = (seconds / total_seconds * 100) if total_seconds > 0 else 0
        bar_len = int(pct / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        lines.append(f"{row['category']:<25} {hours}h {mins:02d}m  {bar}  {pct:.0f}%")

    total_h = total_seconds // 3600
    total_m = (total_seconds % 3600) // 60
    lines.append("=" * 50)
    lines.append(f"Total tracked: {total_h}h {total_m:02d}m")

    if detailed:
        spans = get_spans_for_date(d, DB_PATH)
        if spans:
            lines.append("")
            lines.append("Detailed Timeline:")
            lines.append("-" * 50)
            for span in spans:
                start = span.start_time.strftime("%H:%M")
                end = span.end_time.strftime("%H:%M")
                dur_m = span.duration_seconds // 60
                desc = span.description or span.window_title or span.app_name
                cat = span.category or "Unclassified"
                lines.append(f"  {start}-{end} ({dur_m}m) [{cat}] {desc}")

    return "\n".join(lines)


@mcp.tool()
def list_categories() -> str:
    """List all available activity categories."""
    init_db(DB_PATH)
    categories = get_categories(DB_PATH)
    return json.dumps(categories, indent=2)


@mcp.tool()
def create_category(name: str, color: str = "#7f8c8d", productive: Optional[bool] = None) -> str:
    """Add a new activity category.

    Args:
        name: Category name (e.g., "Design", "Admin")
        color: Hex color code
        productive: Whether this category is productive (true/false/null for neutral)
    """
    init_db(DB_PATH)
    is_prod = None
    if productive is True:
        is_prod = 1
    elif productive is False:
        is_prod = 0
    add_category(name, color, is_prod, DB_PATH)
    return f"Category '{name}' created."


@mcp.tool()
def get_tracker_status() -> str:
    """Check the status of the activity tracker — database stats, storage usage, etc."""
    init_db(DB_PATH)
    from tracker.cleanup import get_storage_stats
    from tracker.db import get_connection

    conn = get_connection(DB_PATH)
    latest = conn.execute("SELECT MAX(timestamp) FROM observations").fetchone()[0]
    earliest = conn.execute("SELECT MIN(timestamp) FROM observations").fetchone()[0]
    conn.close()

    stats = get_storage_stats(DB_PATH)
    stats["earliest_capture"] = earliest
    stats["latest_capture"] = latest
    stats["database_path"] = DB_PATH
    stats["retention_policy"] = {
        "screenshots": "7 days",
        "observations": "30 days (classified only)",
        "spans": "kept forever",
    }

    return json.dumps(stats, indent=2)


@mcp.tool()
def run_storage_cleanup(
    keep_screenshots_days: int = 7,
    keep_observations_days: int = 30,
) -> str:
    """Run storage cleanup to free disk space.

    Args:
        keep_screenshots_days: Keep screenshots for this many days (default 7).
        keep_observations_days: Keep raw observations for this many days (default 30).
            Only deletes classified observations — unclassified ones are always kept.

    Retention tiers:
      - Last 7 days: everything kept (screenshots + observations + spans)
      - 7-30 days: screenshots deleted, observations + spans kept
      - 30+ days: classified observations deleted, only spans remain
    """
    init_db(DB_PATH)
    from tracker.cleanup import get_storage_stats, run_cleanup

    before = get_storage_stats(DB_PATH)
    run_cleanup(DB_PATH, keep_screenshots_days=keep_screenshots_days, keep_observations_days=keep_observations_days)
    after = get_storage_stats(DB_PATH)

    freed_mb = before["total_size_mb"] - after["total_size_mb"]
    return json.dumps({
        "freed_mb": round(freed_mb, 1),
        "before": {"total_mb": before["total_size_mb"], "screenshots": before["screenshot_count"]},
        "after": {"total_mb": after["total_size_mb"], "screenshots": after["screenshot_count"]},
        "retention": {
            "keep_screenshots_days": keep_screenshots_days,
            "keep_observations_days": keep_observations_days,
        },
    }, indent=2)


@mcp.tool()
def get_screenshot(observation_id: int) -> list[TextContent | ImageContent]:
    """Get the screenshot image for a specific observation.

    Args:
        observation_id: The observation ID that has a screenshot.

    Returns the screenshot as an image that can be viewed directly.
    Use this to see exactly what was on screen at a given moment —
    useful for reading email subjects, document content, etc.
    """
    init_db(DB_PATH)
    from tracker.db import get_connection

    conn = get_connection(DB_PATH)
    row = conn.execute(
        "SELECT screenshot_path, timestamp, app_name, window_title FROM observations WHERE id = ?",
        (observation_id,),
    ).fetchone()
    conn.close()

    if not row:
        return [TextContent(type="text", text=f"No observation found with ID {observation_id}")]

    screenshot_path = row["screenshot_path"]
    if not screenshot_path or not os.path.exists(screenshot_path):
        return [TextContent(type="text", text=f"No screenshot available for observation {observation_id} ({row['app_name']} at {row['timestamp']})")]

    with open(screenshot_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    return [
        TextContent(
            type="text",
            text=f"Screenshot from {row['timestamp']} — App: {row['app_name']}, Window: {row['window_title'] or 'N/A'}",
        ),
        ImageContent(type="image", data=image_data, mimeType="image/jpeg"),
    ]


@mcp.tool()
def get_screenshots_for_period(start_time: str = "", end_time: str = "", limit: int = 5) -> str:
    """List observations that have screenshots for a given time period.

    Args:
        start_time: Start time in HH:MM format (defaults to 1 hour ago).
        end_time: End time in HH:MM format (defaults to now).
        limit: Max number of results (default 5).

    Returns a list of observation IDs with screenshots. Use get_screenshot
    to view any specific one.
    """
    init_db(DB_PATH)
    from tracker.db import get_connection

    conn = get_connection(DB_PATH)

    if start_time and end_time:
        today = date_mod.today().isoformat()
        rows = conn.execute(
            """SELECT id, timestamp, app_name, window_title, screenshot_path
               FROM observations
               WHERE screenshot_path IS NOT NULL
               AND timestamp >= ? AND timestamp <= ?
               ORDER BY timestamp DESC LIMIT ?""",
            (f"{today}T{start_time}:00", f"{today}T{end_time}:59", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, timestamp, app_name, window_title, screenshot_path
               FROM observations
               WHERE screenshot_path IS NOT NULL
               AND timestamp >= datetime('now', 'localtime', '-1 hour')
               ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    conn.close()

    if not rows:
        return "No screenshots found for this period."

    results = []
    for row in rows:
        results.append({
            "observation_id": row["id"],
            "timestamp": row["timestamp"],
            "app": row["app_name"],
            "window": row["window_title"],
        })

    return json.dumps(results, indent=2)


@mcp.tool()
def search_activity(query: str, target_date: str = "", limit: int = 20) -> str:
    """Full-text search across activity spans and captured web page content.

    Args:
        query: Search terms. Supports FTS5 syntax (AND, OR, NOT, "phrases").
        target_date: Optional date filter (YYYY-MM-DD). Omit to search all dates.
        limit: Max results per category (default 20).

    Use this to answer questions like "what did I read about Project Phoenix?",
    "when did I work on ServiceNow tickets?", etc.
    """
    init_db(DB_PATH)
    from tracker.db import search_spans_fts, search_web_pages, search_people_db

    d = date_mod.fromisoformat(target_date) if target_date else None

    spans = search_spans_fts(query, limit, d, DB_PATH)
    pages = search_web_pages(query, limit, d, DB_PATH)
    people = search_people_db(query, DB_PATH)

    result = {"spans": spans, "web_pages": pages, "people": people}
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_day_context(target_date: str = "") -> str:
    """Get comprehensive context for a day — spans, people, web pages, stats.

    Designed to give Claude enough context to answer questions like
    "What did I work on today?", "Who did I meet with?",
    "What was that article about X?".

    Args:
        target_date: Date in YYYY-MM-DD format. Defaults to today.
    """
    init_db(DB_PATH)
    from tracker.db import (get_spans_for_date, get_web_pages_for_date,
                            get_category_breakdown, get_connection)

    d = date_mod.fromisoformat(target_date) if target_date else date_mod.today()

    spans = get_spans_for_date(d, DB_PATH)
    pages = get_web_pages_for_date(d, DB_PATH)
    breakdown = get_category_breakdown(d, DB_PATH)

    # Get people seen today
    conn = get_connection(DB_PATH)
    people_rows = conn.execute(
        """SELECT DISTINCT p.id, p.name, pa.interaction_type, pa.context
           FROM person_activity pa
           JOIN people p ON p.id = pa.person_id
           WHERE pa.timestamp >= ? AND pa.timestamp < date(?, '+1 day')
           ORDER BY pa.timestamp""",
        (d.isoformat(), d.isoformat()),
    ).fetchall()
    conn.close()

    result = {
        "date": d.isoformat(),
        "spans": [{
            "start": s.start_time.strftime("%H:%M"),
            "end": s.end_time.strftime("%H:%M"),
            "duration_min": round(s.duration_seconds / 60),
            "app": s.app_name,
            "category": s.category,
            "subcategory": s.subcategory,
            "description": s.description,
        } for s in spans],
        "web_pages": pages,
        "people": [dict(r) for r in people_rows],
        "category_breakdown": [dict(r) for r in breakdown],
    }
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def search_people(query: str) -> str:
    """Search for people/contacts detected in activity data.

    People are auto-detected from Teams chats, meetings, Outlook emails,
    and classified span descriptions.

    Args:
        query: Name or partial name to search for.
    """
    init_db(DB_PATH)
    from tracker.db import search_people_db
    results = search_people_db(query, DB_PATH)
    if not results:
        return f"No people found matching '{query}'."
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def get_person_activity(person_name: str, limit: int = 20) -> str:
    """Get all activity associated with a person — meetings, chats, emails.

    Args:
        person_name: Name of the person to look up.
        limit: Max results (default 20).
    """
    init_db(DB_PATH)
    from tracker.db import search_people_db, get_person_activity_db

    people = search_people_db(person_name, DB_PATH)
    if not people:
        return f"No person found matching '{person_name}'."

    person = people[0]
    activity = get_person_activity_db(person["id"], limit, DB_PATH)

    result = {
        "person": person,
        "activity": activity,
    }
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def search_links(
    query: str,
    date: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Search all captured links/URLs by keyword.

    Searches URL, title, hostname, and surrounding context.
    Great for finding "that link someone sent me" or "the ServiceNow ticket URL".

    Args:
        query: Search term (matches URL, title, hostname, or context).
        date: Optional date filter (YYYY-MM-DD).
        limit: Max results (default 20).
    """
    from tracker.db import search_links as _search_links

    init_db(DB_PATH)
    target = date_mod.fromisoformat(date) if date else None
    results = _search_links(query, limit, target, DB_PATH)
    if not results:
        return f"No links found matching '{query}'."
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def get_links_for_date(
    date: Optional[str] = None,
    min_dwell_minutes: float = 1.0,
) -> str:
    """Get all links visited on a date, ranked by time spent.

    Args:
        date: Date (YYYY-MM-DD). Defaults to today.
        min_dwell_minutes: Minimum minutes spent on a link to include (default 1).
    """
    from tracker.db import get_links_for_date as _get_links

    init_db(DB_PATH)
    target = date_mod.fromisoformat(date) if date else date_mod.today()
    results = _get_links(target, min_dwell_minutes * 60, DB_PATH)
    if not results:
        return f"No links found for {target}."
    return json.dumps(results, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
