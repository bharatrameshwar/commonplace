#!/usr/bin/env python3
"""Activity Tracker Dashboard — lightweight Flask web server.

Reads from the SQLite DB and serves a real-time dashboard at localhost:8420.
Auto-refreshes every 60s. No external API calls — just local data.
"""

import json
import os
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import urlparse, parse_qs

from flask import Flask, jsonify, render_template, send_file

from tracker.config import ai_settings, self_aliases
from tracker.db import (
    DEFAULT_DB_PATH,
    get_categories,
    get_category_breakdown,
    get_connection,
    get_memory_items_for_window,
    get_observations_for_date,
    get_people,
    get_person_activity_db,
    get_spans_for_date,
    get_unclassified_spans,
    get_web_pages_for_date,
    init_db,
    search_people_db,
    search_spans_fts,
    search_web_pages,
)

DB_PATH = os.environ.get("ACTIVITY_TRACKER_DB", DEFAULT_DB_PATH)

# Subjects/titles that aren't real items — generic + the user's own name.
_GENERIC_SKIP_SUBJECTS = {"inbox", "sent items", "drafts", "calendar"}


def _skip_subjects() -> set[str]:
    return _GENERIC_SKIP_SUBJECTS | self_aliases()

app = Flask(__name__)

# Fallback palette for categories not yet in the DB
FALLBACK_COLORS = [
    # Warm palette — harmonizes with the peach/cream gradient background
    "#ea580c",  # burnt orange
    "#b45309",  # amber
    "#9a3412",  # rust
    "#c2410c",  # dark orange
    "#d97706",  # amber-dark
    "#92400e",  # sienna
    "#78350f",  # chocolate
    "#f59e0b",  # amber
    "#a16207",  # dark gold
    "#7c2d12",  # deep rust
    "#854d0e",  # olive-gold
    "#991b1b",  # brick red (used sparingly for distractions)
    "#385e81",  # dusty slate blue (cool accent, replaces old plum)
    "#0f766e",  # muted teal (cool accent)
    "#115e59",  # dark teal
]
_color_idx = 0


def get_fallback_color():
    global _color_idx
    color = FALLBACK_COLORS[_color_idx % len(FALLBACK_COLORS)]
    _color_idx += 1
    return color


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/summary")
def api_summary():
    """Main dashboard data: category breakdown, timeline, stats."""
    init_db(DB_PATH)
    target = _parse_date()
    conn = get_connection(DB_PATH)

    WORK_START_HOUR = 9
    WORK_END_HOUR = 21

    # Category breakdown (filtered to work hours)
    breakdown_raw = get_category_breakdown(target, DB_PATH)
    categories = {c["name"]: c for c in get_categories(DB_PATH)}

    # Re-compute breakdown from spans filtered to work hours
    spans_all = get_spans_for_date(target, DB_PATH)
    spans_work = [s for s in spans_all
                  if WORK_START_HOUR <= s.start_time.hour < WORK_END_HOUR]

    # Build breakdown from work-hours spans
    from collections import defaultdict
    cat_totals = defaultdict(lambda: {"total_seconds": 0, "span_count": 0})
    for s in spans_work:
        cat = s.category or "Unclassified"
        cat_totals[cat]["total_seconds"] += s.duration_seconds
        cat_totals[cat]["span_count"] += 1
    breakdown = [{"category": k, **v} for k, v in
                 sorted(cat_totals.items(), key=lambda x: -x[1]["total_seconds"])]

    category_data = []
    idle_categories = {"Idle/AFK", "Break", "Idle"}
    total_seconds = sum(r["total_seconds"] for r in breakdown
                        if (r["category"] or "Unclassified") not in idle_categories) if breakdown else 0
    for row in breakdown:
        cat_name = row["category"] or "Unclassified"
        cat_info = categories.get(cat_name, {})
        color = cat_info.get("color") or get_fallback_color()
        category_data.append({
            "category": cat_name,
            "seconds": row["total_seconds"],
            "span_count": row["span_count"],
            "color": color,
            "is_productive": cat_info.get("is_productive"),
        })

    # Classified spans for timeline (work hours only)
    timeline = []
    for span in spans_work:
        cat_info = categories.get(span.category or "", {})
        timeline.append({
            "id": span.id,
            "start": span.start_time.strftime("%H:%M"),
            "end": span.end_time.strftime("%H:%M"),
            "duration_minutes": round(span.duration_seconds / 60, 1),
            "app": span.app_name,
            "window": span.window_title,
            "category": span.category or "Unclassified",
            "subcategory": span.subcategory,
            "description": span.description or span.window_title or span.app_name,
            "color": cat_info.get("color", "#7f8c8d"),
            "observation_ids": span.observation_ids,
        })

    # Active window time — from classified spans only (local classifier runs every 5 min)
    first_obs_time = None
    last_obs_time = None
    if spans_work:
        first_obs_time = spans_work[0].start_time.strftime("%I:%M %p")
        last_obs_time = spans_work[-1].end_time.strftime("%I:%M %p")

    # Unclassified count
    unclassified_spans = get_unclassified_spans(DB_PATH)
    unclassified_count = len([s for s in unclassified_spans
                              if s.start_time.date() == target])

    # Observation count for work hours
    observations = get_observations_for_date(target, DB_PATH)
    work_obs = [o for o in observations
                if WORK_START_HOUR <= o.timestamp.hour < WORK_END_HOUR and not o.is_idle]
    obs_count = len(work_obs)

    conn.close()

    # Wall-clock from classified spans — merge overlapping spans to get true active time
    wall_clock_seconds = _compute_wall_clock(spans_work) if spans_work else 0

    total_h = wall_clock_seconds // 3600
    total_m = (wall_clock_seconds % 3600) // 60
    all_seconds = sum(r["total_seconds"] for r in breakdown) if breakdown else 0

    return jsonify({
        "date": target.isoformat(),
        "date_display": target.strftime("%A, %d %B"),
        "total_seconds": wall_clock_seconds,
        "total_with_idle": all_seconds,
        "total_display": f"~{total_h}h {total_m:02d}m",
        "active_window": f"{first_obs_time} - {last_obs_time}" if first_obs_time else "No data",
        "observation_count": obs_count,
        "unclassified_count": unclassified_count,
        "categories": category_data,
        "timeline": timeline,
    })


PURPOSEFUL_CATS = {
    "Development", "Research", "Documentation", "Deep Work",
    "Meetings", "Meetings/Calls", "Planning", "Communication",
}
DEEP_WORK_CATS = {"Development", "Research", "Documentation", "Planning", "Deep Work"}
DISTRACTION_CATS = {"Personal", "Entertainment", "Break"}


def _daily_metrics(day: date, conn) -> dict:
    """Compute per-day metrics from activity_spans.

    Returns: {
        'date': 'YYYY-MM-DD',
        'total_purposeful_sec': int,
        'longest_deep_block_sec': int,
        'longest_deep_desc': str | None,
        'focus_score': int | None,
        'switches': int,
        'best_hour_sec': int,
        'best_hour': int | None,  # 0-23
    }
    """
    day_str = day.isoformat()
    rows = conn.execute(
        "SELECT start_time, end_time, duration_seconds, category, description "
        "FROM activity_spans WHERE date(start_time) = ? ORDER BY start_time",
        (day_str,),
    ).fetchall()

    metrics = {
        "date": day_str,
        "total_purposeful_sec": 0,
        "longest_deep_block_sec": 0,
        "longest_deep_desc": None,
        "focus_score": None,
        "switches": 0,
        "best_hour_sec": 0,
        "best_hour": None,
    }
    if not rows:
        return metrics

    # Totals + longest deep block (single span)
    total_sec = 0
    purposeful_sec = 0
    distraction_sec = 0
    longest_deep = 0
    longest_deep_desc = None
    per_hour = defaultdict(int)  # hour → purposeful seconds

    prev_cat = None
    for r in rows:
        cat = r["category"] or ""
        dur = r["duration_seconds"] or 0
        total_sec += dur

        if cat in PURPOSEFUL_CATS:
            purposeful_sec += dur
            # Split duration across hours for best-hour metric
            try:
                st = datetime.fromisoformat(r["start_time"])
                et = datetime.fromisoformat(r["end_time"])
            except Exception:
                st = et = None
            if st and et:
                cursor = st
                while cursor < et:
                    h = cursor.hour
                    hour_end = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                    chunk_end = min(et, hour_end)
                    per_hour[h] += int((chunk_end - cursor).total_seconds())
                    cursor = chunk_end

        if cat in DEEP_WORK_CATS and dur > longest_deep:
            longest_deep = dur
            longest_deep_desc = r["description"] or cat

        if cat in DISTRACTION_CATS:
            distraction_sec += dur

        if prev_cat is not None and cat != prev_cat:
            metrics["switches"] += 1
        prev_cat = cat

    # Focus score (mirrors frontend formula)
    if total_sec > 0:
        p_ratio = purposeful_sec / total_sec
        d_ratio = distraction_sec / total_sec
        max_switches = 40
        switch_score = max(0.0, 1 - metrics["switches"] / max_switches)
        metrics["focus_score"] = round(p_ratio * 40 + switch_score * 30 + (1 - d_ratio) * 30)

    metrics["total_purposeful_sec"] = purposeful_sec
    metrics["longest_deep_block_sec"] = longest_deep
    metrics["longest_deep_desc"] = longest_deep_desc

    if per_hour:
        best_h, best_s = max(per_hour.items(), key=lambda kv: kv[1])
        metrics["best_hour"] = best_h
        metrics["best_hour_sec"] = best_s

    return metrics


@app.route("/api/records")
def api_records():
    """Detect personal records — today vs rolling 7-day / 30-day history.

    Query params:
      date: YYYY-MM-DD (defaults to today)

    Returns a list of records broken today, each with:
      metric (key), label (text), value, prev_best, prev_best_date, window (7|30)
    """
    target_str = _get_query_date()
    target = date.fromisoformat(target_str) if target_str else date.today()

    conn = get_connection(DB_PATH)
    today_m = _daily_metrics(target, conn)

    records = []
    if not today_m["focus_score"] and today_m["total_purposeful_sec"] == 0:
        conn.close()
        return jsonify(records)

    # Compare to each window
    for window in (7, 30):
        hist_start = target - timedelta(days=window)
        # All historical days strictly BEFORE target
        hist_rows = conn.execute(
            "SELECT DISTINCT date(start_time) AS d FROM activity_spans "
            "WHERE date(start_time) >= ? AND date(start_time) < ?",
            (hist_start.isoformat(), target.isoformat()),
        ).fetchall()
        hist_days = [date.fromisoformat(r["d"]) for r in hist_rows]
        if not hist_days:
            continue
        hist = [_daily_metrics(d, conn) for d in hist_days]

        # 1) Longest deep-work block
        prev_deep = max(((m["longest_deep_block_sec"], m["date"]) for m in hist), default=(0, None))
        if today_m["longest_deep_block_sec"] > prev_deep[0] and today_m["longest_deep_block_sec"] >= 600:
            records.append({
                "metric": "longest_deep_block",
                "label": "Longest deep-work block",
                "value_sec": today_m["longest_deep_block_sec"],
                "value_display": _fmt_dur(today_m["longest_deep_block_sec"]),
                "prev_best_sec": prev_deep[0],
                "prev_best_display": _fmt_dur(prev_deep[0]) if prev_deep[0] else "—",
                "prev_best_date": prev_deep[1],
                "detail": today_m["longest_deep_desc"],
                "window_days": window,
            })
            break  # record found; don't double-report across windows

    for window in (7, 30):
        hist_start = target - timedelta(days=window)
        hist_rows = conn.execute(
            "SELECT DISTINCT date(start_time) AS d FROM activity_spans "
            "WHERE date(start_time) >= ? AND date(start_time) < ?",
            (hist_start.isoformat(), target.isoformat()),
        ).fetchall()
        hist_days = [date.fromisoformat(r["d"]) for r in hist_rows]
        if not hist_days:
            continue
        hist = [_daily_metrics(d, conn) for d in hist_days]

        # 2) Focus score
        prev_focus = max(((m["focus_score"] or 0, m["date"]) for m in hist), default=(0, None))
        if today_m["focus_score"] is not None and today_m["focus_score"] > prev_focus[0]:
            records.append({
                "metric": "focus_score",
                "label": "Highest focus score",
                "value_sec": today_m["focus_score"],
                "value_display": str(today_m["focus_score"]),
                "prev_best_sec": prev_focus[0],
                "prev_best_display": str(prev_focus[0]),
                "prev_best_date": prev_focus[1],
                "detail": None,
                "window_days": window,
            })
            break

    for window in (7, 30):
        hist_start = target - timedelta(days=window)
        hist_rows = conn.execute(
            "SELECT DISTINCT date(start_time) AS d FROM activity_spans "
            "WHERE date(start_time) >= ? AND date(start_time) < ?",
            (hist_start.isoformat(), target.isoformat()),
        ).fetchall()
        hist_days = [date.fromisoformat(r["d"]) for r in hist_rows]
        if not hist_days:
            continue
        hist = [_daily_metrics(d, conn) for d in hist_days]

        # 3) Most purposeful time in a single day
        prev_p = max(((m["total_purposeful_sec"], m["date"]) for m in hist), default=(0, None))
        if today_m["total_purposeful_sec"] > prev_p[0] and today_m["total_purposeful_sec"] >= 3600:
            records.append({
                "metric": "purposeful_total",
                "label": "Most purposeful time in a day",
                "value_sec": today_m["total_purposeful_sec"],
                "value_display": _fmt_dur(today_m["total_purposeful_sec"]),
                "prev_best_sec": prev_p[0],
                "prev_best_display": _fmt_dur(prev_p[0]) if prev_p[0] else "—",
                "prev_best_date": prev_p[1],
                "detail": None,
                "window_days": window,
            })
            break

    conn.close()
    return jsonify(records)


def _fmt_dur(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _get_query_date() -> str:
    from flask import request
    return request.args.get("date", "").strip()


@app.route("/api/screenshots/<int:observation_id>")
def api_screenshot(observation_id):
    """Serve a screenshot image by observation ID."""
    init_db(DB_PATH)
    conn = get_connection(DB_PATH)
    row = conn.execute(
        "SELECT screenshot_path FROM observations WHERE id = ?",
        (observation_id,),
    ).fetchone()
    conn.close()

    if not row or not row["screenshot_path"] or not os.path.exists(row["screenshot_path"]):
        return "Not found", 404
    return send_file(row["screenshot_path"], mimetype="image/jpeg")


@app.route("/api/recent_screenshots")
def api_recent_screenshots():
    """Get the most recent screenshots for the live preview."""
    init_db(DB_PATH)
    conn = get_connection(DB_PATH)
    rows = conn.execute(
        """SELECT id, timestamp, app_name, window_title
           FROM observations
           WHERE screenshot_path IS NOT NULL
           ORDER BY timestamp DESC LIMIT 6""",
    ).fetchall()
    conn.close()

    return jsonify([{
        "id": r["id"],
        "timestamp": r["timestamp"],
        "app": r["app_name"],
        "window": r["window_title"],
    } for r in rows])


@app.route("/api/quick_recall")
def api_quick_recall():
    """Top searchable items from the last 7 days — tickets, emails, people, docs, links."""
    init_db(DB_PATH)
    conn = get_connection(DB_PATH)
    today = date.today()
    week_ago = today - timedelta(days=7)

    chips = []

    # Tickets from spans
    ticket_re = re.compile(
        r'(CS\d{10,}|SR\s*\d{6,}|INC\d{6,}|DINC\d{6,}|CHG\d{6,}|PRB\d{6,})',
        re.IGNORECASE
    )
    # Case numbers in N/YYYY format (e.g. 88936/2026) — common in some
    # ticketing systems. Adjust per your environment if needed.
    case_re = re.compile(r'\b(\d{4,6}/\d{4})\b')

    rows = conn.execute(
        "SELECT window_title, description, browser_url FROM activity_spans WHERE start_time >= ?",
        (week_ago.isoformat(),),
    ).fetchall()

    # Also scan raw observations for case numbers + ticket IDs
    obs_titles = conn.execute(
        "SELECT DISTINCT window_title FROM observations WHERE timestamp >= ? AND "
        "(window_title LIKE '%CS2026%' OR window_title LIKE '%DINC%' OR window_title LIKE '%/%' AND window_title LIKE '%Configurable Workspace%')",
        (week_ago.isoformat(),),
    ).fetchall()

    seen_tickets = set()
    # Map CS-style ticket IDs to N/YYYY case numbers by proximity in observations
    cs_to_case = {}

    # Extract case numbers from observation titles
    for r in obs_titles:
        title = r["window_title"] or ""
        case_match = case_re.search(title)
        if case_match:
            case_num = case_match.group(1)
            # Find the nearest CS ticket ID observed around the same time
            cs_match = ticket_re.search(title)
            if cs_match:
                cs_to_case[cs_match.group(1)] = case_num
            elif case_num not in seen_tickets:
                # Standalone case number
                seen_tickets.add(case_num)
                url_row = conn.execute(
                    "SELECT browser_url FROM observations WHERE window_title LIKE ? AND browser_url LIKE 'http%' LIMIT 1",
                    (f"%{case_num}%",),
                ).fetchone()
                chips.append({
                    "type": "ticket", "icon": "\U0001f3ab", "label": f"Case {case_num}",
                    "url": url_row["browser_url"] if url_row else None, "score": 90,
                })

    for r in rows:
        for text in [r["window_title"] or "", r["description"] or "", r["browser_url"] or ""]:
            for m in ticket_re.finditer(text):
                tid = m.group(1).strip()
                if tid.lower() not in seen_tickets:
                    seen_tickets.add(tid.lower())
                    # Skip CS* and DINC* IDs — only case numbers matter
                    if tid.upper().startswith(("CS", "DINC")):
                        continue
                    url_row = conn.execute(
                        "SELECT browser_url FROM observations WHERE window_title LIKE ? AND browser_url LIKE 'http%' LIMIT 1",
                        (f"%{tid}%",),
                    ).fetchone()
                    label = tid
                    chips.append({
                        "type": "ticket", "icon": "\U0001f3ab", "label": label,
                        "url": url_row["browser_url"] if url_row else None, "score": 100,
                    })

    # Top emails by dwell (from observations)
    email_re = re.compile(r'^(?:Mail|Reading Pane)\s*-\s*(.+?)\s*-\s*Outlook', re.IGNORECASE)
    email_counts = defaultdict(lambda: {"count": 0, "subject": "", "url": None})
    obs_rows = conn.execute(
        "SELECT window_title, browser_url FROM observations WHERE timestamp >= ? AND window_title LIKE '%Outlook%'",
        (week_ago.isoformat(),),
    ).fetchall()
    skip_subjects = _skip_subjects()
    for r in obs_rows:
        m = email_re.match(r["window_title"] or "")
        if m:
            subj = m.group(1).strip()
            if subj.lower() not in skip_subjects:
                key = subj.lower()
                email_counts[key]["count"] += 1
                email_counts[key]["subject"] = subj
                if r["browser_url"] and "outlook" in (r["browser_url"] or "").lower() and "/id/" in (r["browser_url"] or ""):
                    email_counts[key]["url"] = r["browser_url"]

    for e in sorted(email_counts.values(), key=lambda x: -x["count"])[:6]:
        chips.append({
            "type": "email", "icon": "\u2709\ufe0f", "label": e["subject"],
            "url": e["url"], "score": e["count"],
        })

    # People
    people_rows = conn.execute(
        "SELECT name, interaction_count FROM people ORDER BY last_seen DESC LIMIT 8",
    ).fetchall()
    for p in people_rows:
        chips.append({
            "type": "person", "icon": "\U0001f464", "label": p["name"],
            "action": "people", "score": p["interaction_count"],
        })

    # Documents — extract filenames, try to find URLs or local paths
    doc_re = re.compile(r'([\w\s\-().]+\.(?:pptx?|docx?|xlsx?|pdf|csv))', re.IGNORECASE)
    doc_info = {}  # filename -> {count, url}
    for r in rows:
        for text in [r["window_title"] or "", r["description"] or ""]:
            for m in doc_re.finditer(text):
                fname = m.group(1).strip()
                # Clean up trailing " - Comet", " - PowerPoint", etc.
                fname = re.sub(r'\s*-\s*(Comet|PowerPoint|Excel|Word|Preview|Keynote|Numbers)$', '', fname, flags=re.IGNORECASE).strip()
                if len(fname) < 5:
                    continue
                if fname not in doc_info:
                    doc_info[fname] = {"count": 0, "url": None}
                doc_info[fname]["count"] += 1

    # Try to find browser URLs for these docs
    for fname in doc_info:
        url_row = conn.execute(
            "SELECT browser_url FROM observations WHERE browser_url LIKE 'http%' AND window_title LIKE ? LIMIT 1",
            (f"%{fname}%",),
        ).fetchone()
        if url_row:
            doc_info[fname]["url"] = url_row["browser_url"]

    # Also check for local file paths from Finder
    finder_rows = conn.execute(
        "SELECT DISTINCT window_title FROM observations WHERE app_name = 'Finder' AND timestamp >= ? AND (window_title LIKE '%.pptx%' OR window_title LIKE '%.xlsx%' OR window_title LIKE '%.docx%' OR window_title LIKE '%.pdf%')",
        (week_ago.isoformat(),),
    ).fetchall()
    for fr in finder_rows:
        title = fr["window_title"] or ""
        for m in doc_re.finditer(title):
            fname = m.group(1).strip()
            if fname not in doc_info:
                doc_info[fname] = {"count": 1, "url": None}

    doc_icons = {
        'pptx': '\U0001f4ca', 'ppt': '\U0001f4ca',
        'xlsx': '\U0001f4ca', 'xls': '\U0001f4ca',
        'docx': '\U0001f4c4', 'doc': '\U0001f4c4',
        'pdf': '\U0001f4d5', 'csv': '\U0001f4cb',
    }
    for fname, info in sorted(doc_info.items(), key=lambda x: -x[1]["count"])[:6]:
        ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
        icon = doc_icons.get(ext, '\U0001f4c4')
        chip = {
            "type": "doc", "icon": icon, "label": fname,
            "score": info["count"] * 10,
        }
        if info["url"]:
            chip["url"] = info["url"]
        else:
            chip["action"] = "filter"
        chips.append(chip)

    # Top links by dwell time
    try:
        link_rows = conn.execute(
            "SELECT url, title, total_dwell_seconds FROM links WHERE last_seen >= ? ORDER BY total_dwell_seconds DESC LIMIT 6",
            (week_ago.isoformat(),),
        ).fetchall()
        for l in link_rows:
            if l["total_dwell_seconds"] >= 120:  # 2+ min
                chips.append({
                    "type": "link", "icon": "\U0001f517", "label": l["title"] or l["url"],
                    "url": l["url"], "score": l["total_dwell_seconds"],
                })
    except Exception:
        pass  # links table may not exist yet

    # Search queries
    search_rows = conn.execute(
        "SELECT DISTINCT browser_url, timestamp FROM observations WHERE timestamp >= ? AND browser_url LIKE '%?q=%' ORDER BY timestamp DESC LIMIT 20",
        (week_ago.isoformat(),),
    ).fetchall()
    seen_queries = set()
    for r in search_rows:
        params = parse_qs(urlparse(r["browser_url"]).query)
        q = params.get("q") or params.get("query") or params.get("search")
        if q and q[0] not in seen_queries and len(q[0]) > 2:
            seen_queries.add(q[0])
            chips.append({
                "type": "search", "icon": "\U0001f50d", "label": q[0],
                "action": "filter", "score": 40,
            })
            if len(seen_queries) >= 4:
                break

    conn.close()

    # Deduplicate by label, sort by score, cap at 25
    seen = set()
    unique = []
    for c in sorted(chips, key=lambda x: -x.get("score", 0)):
        key = c["label"].lower()[:50]
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return jsonify(unique[:25])


@app.route("/api/memory")
def api_memory():
    """Memory assistant — surfaces the most useful information for a day."""
    init_db(DB_PATH)
    target = _parse_date()
    conn = get_connection(DB_PATH)

    spans = get_spans_for_date(target, DB_PATH)
    observations = get_observations_for_date(target, DB_PATH)
    pages = get_web_pages_for_date(target, DB_PATH)

    # Helper: find span context for a time range
    def find_span_context(timestamp_str):
        """Find the classified span covering a given time, return description + obs_ids."""
        for s in spans:
            if s.start_time.strftime("%H:%M") <= timestamp_str <= s.end_time.strftime("%H:%M"):
                return {
                    "span_id": s.id,
                    "description": s.description,
                    "category": s.category,
                    "observation_ids": s.observation_ids[:20],  # cap for API size
                }
        return None

    # ── 1. Tickets & Reference IDs ─────────────────────────────────────
    ticket_pattern = re.compile(
        r'(CS\d{10,}|SR\s*\d{6,}|INC\d{6,}|DINC\d{6,}|CHG\d{6,}|PRB\d{6,})',
        re.IGNORECASE
    )
    case_pattern = re.compile(r'\b(\d{4,6}/\d{4})\b')

    # Build a map of CS-style ticket → N/YYYY case number from observations
    cs_case_map = {}
    case_obs = [o for o in observations if "Configurable Workspace" in (o.window_title or "")]
    for o in case_obs:
        title = o.window_title or ""
        cm = case_pattern.search(title)
        tm = ticket_pattern.search(title)
        if cm and tm:
            cs_case_map[tm.group(1)] = cm.group(1)

    tickets = {}
    for s in spans:
        for text in [s.window_title or "", s.description or "", s.browser_url or ""]:
            for match in ticket_pattern.finditer(text):
                tid = match.group(1).strip()
                if tid not in tickets:
                    # Skip CS/DINC IDs — only show case numbers and SR/INC
                    if tid.upper().startswith(("CS", "DINC")):
                        continue
                    label = tid
                    tickets[tid] = {
                        "id": label,
                        "context": s.description or s.window_title or "",
                        "category": s.category,
                        "time": s.start_time.strftime("%H:%M"),
                        "app": s.app_name,
                        "observation_ids": s.observation_ids[:20],
                    }

    # Also surface standalone case numbers not linked to a CS ticket
    for o in case_obs:
        title = o.window_title or ""
        cm = case_pattern.search(title)
        if cm and not ticket_pattern.search(title):
            case_num = cm.group(1)
            case_key = f"case-{case_num}"
            if case_key not in tickets:
                tickets[case_key] = {
                    "id": f"Case {case_num}",
                    "context": title.split("|")[0].strip() if "|" in title else title,
                    "category": "Admin",
                    "time": o.timestamp.strftime("%H:%M"),
                    "app": "ServiceNow",
                    "observation_ids": [],
                }

    # ── 2. Deep Reads (pages with 3+ min dwell) ───────────────────────
    # Estimate dwell time from consecutive observations on same URL
    url_dwell = defaultdict(lambda: {"seconds": 0, "first_seen": None, "last_seen": None})
    for i, obs in enumerate(observations):
        if not obs.browser_url:
            continue
        url = obs.browser_url
        if url_dwell[url]["first_seen"] is None:
            url_dwell[url]["first_seen"] = obs.timestamp
        url_dwell[url]["last_seen"] = obs.timestamp
        # Add interval to next observation (or default 5s)
        if i + 1 < len(observations):
            gap = min((observations[i+1].timestamp - obs.timestamp).total_seconds(), 30)
        else:
            gap = 5
        url_dwell[url]["seconds"] += gap

    deep_reads = []
    page_map = {p["url"]: p for p in pages}
    for url, info in sorted(url_dwell.items(), key=lambda x: -x[1]["seconds"]):
        if info["seconds"] < 180:  # 3+ minutes
            continue
        page = page_map.get(url)
        time_str = info["first_seen"].strftime("%H:%M") if info["first_seen"] else ""
        deep_reads.append({
            "url": url,
            "title": page["title"] if page else urlparse(url).hostname,
            "content_preview": page.get("content_preview", "") if page else "",
            "dwell_minutes": round(info["seconds"] / 60, 1),
            "time": time_str,
            "span_context": find_span_context(time_str),
        })
    deep_reads = deep_reads[:15]

    # ── 3. People & Context ────────────────────────────────────────────
    people_rows = conn.execute(
        """SELECT p.id as person_id, p.name, p.weekly_summary,
                  pa.interaction_type, pa.context,
                  pa.timestamp, s.category, s.description as span_desc
           FROM person_activity pa
           JOIN people p ON p.id = pa.person_id
           LEFT JOIN activity_spans s ON s.id = pa.span_id
           WHERE pa.timestamp >= ? AND pa.timestamp < date(?, '+1 day')
           ORDER BY pa.timestamp""",
        (target.isoformat(), target.isoformat()),
    ).fetchall()
    people_context = []
    seen_people = set()
    for r in people_rows:
        name = r["name"]
        if name in seen_people:
            continue
        seen_people.add(name)
        people_context.append({
            "id": r["person_id"],
            "name": name,
            "type": r["interaction_type"],
            "context": r["span_desc"] or r["context"] or "",
            "category": r["category"],
            "time": r["timestamp"].split("T")[1][:5] if "T" in (r["timestamp"] or "") else "",
            "weekly_summary": r["weekly_summary"],
        })

    # ── 4. Links Shared in Chat ────────────────────────────────────────
    # URLs that appeared during/near Teams/Slack communication spans
    chat_spans = [s for s in spans if s.category in ("Communication",) and
                  any(k in (s.app_name or "").lower() for k in ("teams", "slack", "discord"))]
    chat_links = []
    for cs in chat_spans:
        # Find browser observations within the chat span's time range (+/- 2 min)
        for obs in observations:
            if obs.browser_url and not obs.is_idle:
                if (cs.start_time - timedelta(minutes=2)) <= obs.timestamp <= (cs.end_time + timedelta(minutes=2)):
                    parsed = urlparse(obs.browser_url)
                    title = obs.browser_tab_title or parsed.hostname or ""
                    # Skip generic/noisy links + the user's own name
                    _skip_titles = {"inbox", "calendar", "new tab",
                                    "sent items", "drafts"} | self_aliases()
                    _skip_hosts = {"teams.microsoft.com", "outlook.office.com",
                                   "outlook.cloud.microsoft", "login.microsoftonline.com",
                                   "mail.google.com"}
                    if (parsed.hostname and parsed.hostname not in _skip_hosts
                            and not any(s in title.lower() for s in _skip_titles)):
                        chat_links.append({
                            "url": obs.browser_url,
                            "title": title,
                            "during_chat_with": cs.description or cs.window_title or "",
                            "time": obs.timestamp.strftime("%H:%M"),
                            "observation_ids": cs.observation_ids[:20],
                        })
    # Deduplicate
    seen_urls = set()
    unique_chat_links = []
    for cl in chat_links:
        if cl["url"] not in seen_urls:
            seen_urls.add(cl["url"])
            unique_chat_links.append(cl)
    chat_links = unique_chat_links[:10]

    # ── 5. Documents Worked On ─────────────────────────────────────────
    doc_pattern = re.compile(r'(.+?\.(?:pptx?|docx?|xlsx?|pdf|csv))', re.IGNORECASE)
    docs = {}
    for s in spans:
        for text in [s.window_title or "", s.description or ""]:
            for match in doc_pattern.finditer(text):
                fname = match.group(1).strip()
                if fname not in docs:
                    docs[fname] = {"name": fname, "total_seconds": 0, "app": s.app_name,
                                   "category": s.category, "first_time": s.start_time.strftime("%H:%M")}
                docs[fname]["total_seconds"] += s.duration_seconds
    documents = sorted(docs.values(), key=lambda x: -x["total_seconds"])[:10]
    for d in documents:
        d["duration_display"] = f"{d['total_seconds'] // 60}m"

    # ── 6. Emails worked on ────────────────────────────────────────────
    # Group Outlook observations by email subject, rank by dwell time
    email_pattern = re.compile(
        r'^(?:Mail|Reading Pane)\s*-\s*(.+?)\s*-\s*Outlook',
        re.IGNORECASE,
    )
    # Also match JIRA-style email subjects
    jira_email_pattern = re.compile(r'^\[([^\]]+)\]\s*(.+?)(?:\s*-\s*(?:SAPJIRA|Jira))', re.IGNORECASE)

    email_dwell = defaultdict(lambda: {"seconds": 0, "subject": "", "first_time": "", "obs_ids": []})
    for i, obs in enumerate(observations):
        title = obs.window_title or ""
        subject = None

        m = email_pattern.match(title)
        if m:
            subject = m.group(1).strip()
            # Skip generic views (Inbox, Sent Items, Calendar, folder names without subjects)
            if subject.lower() in _skip_subjects():
                continue

        if not subject:
            m = jira_email_pattern.match(title)
            if m:
                subject = f"[{m.group(1)}] {m.group(2).strip()}"

        if subject:
            key = subject.lower()
            if not email_dwell[key]["subject"]:
                email_dwell[key]["subject"] = subject
                email_dwell[key]["first_time"] = obs.timestamp.strftime("%H:%M")
            # Capture Outlook deep link URL if available
            if obs.browser_url and "outlook" in (obs.browser_url or "").lower() and "/id/" in (obs.browser_url or ""):
                email_dwell[key]["url"] = obs.browser_url
            if i + 1 < len(observations):
                gap = min((observations[i+1].timestamp - obs.timestamp).total_seconds(), 30)
            else:
                gap = 5
            email_dwell[key]["seconds"] += gap
            if obs.id:
                email_dwell[key]["obs_ids"].append(obs.id)

    emails = [v for v in sorted(email_dwell.values(), key=lambda x: -x["seconds"]) if v["seconds"] >= 60]
    for e in emails:
        e["dwell_minutes"] = round(e["seconds"] / 60, 1)
        e["obs_ids"] = e["obs_ids"][:20]
    emails = emails[:15]

    # ── 7. Try to find URLs for ticket IDs from observations ───────────
    for tid_key, ticket in tickets.items():
        for obs in observations:
            if obs.browser_url and tid_key in (obs.window_title or "") and obs.browser_url.startswith("http"):
                ticket["url"] = obs.browser_url
                break

    # ── 8. Search Queries ──────────────────────────────────────────────
    search_queries = []
    seen_queries = set()
    for obs in observations:
        if not obs.browser_url:
            continue
        parsed = urlparse(obs.browser_url)
        params = parse_qs(parsed.query)
        q = params.get("q") or params.get("query") or params.get("search") or params.get("search_query")
        if q:
            query_text = q[0]
            if query_text not in seen_queries and len(query_text) > 2:
                seen_queries.add(query_text)
                search_queries.append({
                    "query": query_text,
                    "source": parsed.hostname or "",
                    "time": obs.timestamp.strftime("%H:%M"),
                })
    search_queries = search_queries[:15]

    # ── 9. App Discovery (first-time apps/sites) ──────────────────────
    # Find apps/domains used today that weren't used in the previous 7 days
    prev_start = (target - timedelta(days=7)).isoformat()
    prev_end = target.isoformat()
    prev_apps = {r[0] for r in conn.execute(
        "SELECT DISTINCT app_name FROM observations WHERE timestamp >= ? AND timestamp < ?",
        (prev_start, prev_end),
    ).fetchall()}
    prev_domains = {r[0] for r in conn.execute(
        "SELECT DISTINCT browser_url FROM observations WHERE timestamp >= ? AND timestamp < ? AND browser_url IS NOT NULL",
        (prev_start, prev_end),
    ).fetchall()}
    prev_hostnames = set()
    for u in prev_domains:
        try:
            prev_hostnames.add(urlparse(u).hostname)
        except Exception:
            pass

    # Skip system/background apps and internal browser pages
    _skip_apps = {"app_mode_loader", "universalcontrol", "usernotificationcenter",
                  "screencapturekit", "control center", "loginwindow", "dock",
                  "windowmanager", "spotlight", "notification center", "SystemUIServer",
                  "CoreServicesUIAgent", "AirPlayUIAgent", "TextInputMenuAgent",
                  "com.apple", "WiFiAgent", "storedownloadd"}
    _skip_domains = {"saml", "login", "auth", "sso", "localhost", "127.0.0.1",
                     "chrome-extension", "about:blank", "newtab"}

    today_apps = set()
    today_domains = set()
    new_apps = []
    new_sites = []
    for obs in observations:
        if obs.app_name not in today_apps:
            today_apps.add(obs.app_name)
            if obs.app_name not in prev_apps:
                # Filter out system/background processes
                if not any(skip in obs.app_name.lower() for skip in _skip_apps):
                    new_apps.append({"name": obs.app_name, "time": obs.timestamp.strftime("%H:%M")})
        if obs.browser_url:
            try:
                hostname = urlparse(obs.browser_url).hostname
                if hostname and hostname not in today_domains:
                    today_domains.add(hostname)
                    if hostname not in prev_hostnames:
                        # Filter out auth/login/internal pages
                        if not any(skip in hostname.lower() for skip in _skip_domains):
                            new_sites.append({"domain": hostname, "time": obs.timestamp.strftime("%H:%M"),
                                              "title": obs.browser_tab_title or hostname})
            except Exception:
                pass

    # ── 11. End of Day Diff ────────────────────────────────────────────
    morning_spans = [s for s in spans if s.start_time.hour < 12]
    afternoon_spans = [s for s in spans if s.start_time.hour >= 12]
    def cat_summary(span_list):
        totals = defaultdict(int)
        for s in span_list:
            totals[s.category or "Unclassified"] += s.duration_seconds
        return dict(sorted(totals.items(), key=lambda x: -x[1]))
    day_diff = {
        "morning": cat_summary(morning_spans),
        "afternoon": cat_summary(afternoon_spans),
    }

    # ── 12. Highlights — curated top items ───────────────────────────
    # Score and rank the most noteworthy items from the day
    highlights = []

    # A) Focus blocks — spans 20+ min, not Communication/Admin
    focus_cats = {"Development", "Research", "Documentation", "Planning"}
    for s in sorted(spans, key=lambda x: -x.duration_seconds):
        if s.duration_seconds >= 1200 and s.category in focus_cats:
            highlights.append({
                "type": "focus",
                "icon": "🎯",
                "title": s.description or s.window_title or s.app_name,
                "subtitle": f"{s.category} · {s.duration_seconds // 60}m focus block",
                "time": s.start_time.strftime("%H:%M"),
                "score": s.duration_seconds * 1.5,  # weight focus blocks heavily
                "observation_ids": s.observation_ids[:20] if s.observation_ids else [],
            })

    # B) Key meetings — meetings 15+ min
    for s in spans:
        if s.category in ("Meetings", "Meetings/Calls") and s.duration_seconds >= 900:
            highlights.append({
                "type": "meeting",
                "icon": "📞",
                "title": s.description or s.window_title or "Meeting",
                "subtitle": f"{s.duration_seconds // 60}m call",
                "time": s.start_time.strftime("%H:%M"),
                "score": s.duration_seconds * 1.2,
                "observation_ids": s.observation_ids[:20] if s.observation_ids else [],
            })

    # C) Tickets worked on — from tickets dict, boost if multiple spans reference it
    for tid, t in tickets.items():
        # Count how many spans mention this ticket
        mention_count = sum(1 for s in spans if tid in (s.window_title or "") + (s.description or ""))
        highlights.append({
            "type": "ticket",
            "icon": "🎫",
            "title": f"{tid}",
            "subtitle": t["context"][:80],
            "time": t["time"],
            "url": t.get("url"),
            "score": 600 + mention_count * 300,  # base + boost per mention
            "observation_ids": t.get("observation_ids", []),
        })

    # D) Top emails by dwell time (3+ min)
    for e in emails[:3]:
        if e["seconds"] >= 180:
            highlights.append({
                "type": "email",
                "icon": "✉️",
                "title": e["subject"],
                "subtitle": f"{e['dwell_minutes']}m reading/writing",
                "time": e["first_time"],
                "url": e.get("url"),
                "score": e["seconds"] * 1.0,
                "observation_ids": e.get("obs_ids", []),
            })

    # E) Deep reads (5+ min)
    for r in deep_reads[:3]:
        if r["dwell_minutes"] >= 5:
            highlights.append({
                "type": "read",
                "icon": "📖",
                "title": r["title"] or r["url"],
                "subtitle": f"{r['dwell_minutes']}m reading",
                "time": r["time"],
                "url": r["url"],
                "score": r["dwell_minutes"] * 60 * 0.8,
                "observation_ids": r.get("span_context", {}).get("observation_ids", []) if r.get("span_context") else [],
            })

    # F) People interactions — top people by context richness
    for p in people_context[:3]:
        highlights.append({
            "type": "person",
            "icon": "👤",
            "title": p["name"],
            "subtitle": f"{p['type']} · {p['context'][:60]}",
            "time": p["time"],
            "score": 400,  # flat score, boosted if they appear in meetings
            "observation_ids": [],
        })

    # G) Documents worked on (10+ min)
    for d in documents[:2]:
        if d["total_seconds"] >= 600:
            highlights.append({
                "type": "doc",
                "icon": "📄",
                "title": d["name"],
                "subtitle": f"{d['duration_display']} in {d['app']}",
                "time": d["first_time"],
                "score": d["total_seconds"] * 0.9,
                "observation_ids": [],
            })

    # Sort by score, take top 8, deduplicate by title
    seen_titles = set()
    unique_highlights = []
    for h in sorted(highlights, key=lambda x: -x["score"]):
        key = h["title"].lower()[:40]
        if key not in seen_titles:
            seen_titles.add(key)
            unique_highlights.append(h)
    highlights = unique_highlights[:8]

    # Remove highlighted items from their sections to avoid repetition
    hl_titles = {h["title"].lower()[:40] for h in highlights}
    hl_ticket_ids = {h["title"] for h in highlights if h["type"] == "ticket"}

    filtered_tickets = [t for t in tickets.values() if t["id"] not in hl_ticket_ids]
    filtered_emails = [e for e in emails if e["subject"].lower()[:40] not in hl_titles]
    filtered_deep_reads = [r for r in deep_reads if (r["title"] or "").lower()[:40] not in hl_titles]
    filtered_people = [p for p in people_context if p["name"].lower()[:40] not in hl_titles]
    filtered_documents = [d for d in documents if d["name"].lower()[:40] not in hl_titles]

    conn.close()

    return jsonify({
        "date": target.isoformat(),
        "highlights": highlights,
        "tickets": filtered_tickets,
        "deep_reads": filtered_deep_reads,
        "people": filtered_people,
        "emails": filtered_emails,
        "chat_links": chat_links,
        "documents": filtered_documents,
        "search_queries": search_queries,
        "day_diff": day_diff,
    })


@app.route("/api/memory_window")
def api_memory_window():
    """Aggregate /api/memory data across a rolling window (default 10 days).

    Query params:
      days: number of days back to include (default 10, max 30)
      end: end date YYYY-MM-DD (defaults to today)

    Returns the same shape as /api/memory but with items merged/deduped across the window.
    """
    from flask import request
    init_db(DB_PATH)
    days_back = max(1, min(30, int(request.args.get("days", 10))))
    end_str = request.args.get("end", "")
    end = date.fromisoformat(end_str) if end_str else date.today()
    start = end - timedelta(days=days_back - 1)

    # Call api_memory logic for each day and merge
    # We can't easily reuse the function directly (it reads ?date via _parse_date),
    # so we call the endpoint function and then merge manifests.
    import copy
    with app.test_request_context(f"/api/memory?date={end.isoformat()}"):
        merged = {
            "date": end.isoformat(),
            "window_start": start.isoformat(),
            "window_days": days_back,
            "highlights": [],
            "tickets": [],
            "deep_reads": [],
            "people": [],
            "emails": [],
            "chat_links": [],
            "documents": [],
            "search_queries": [],
            "day_diff": {},
        }

    for i in range(days_back):
        d = start + timedelta(days=i)
        with app.test_request_context(f"/api/memory?date={d.isoformat()}"):
            try:
                resp = api_memory()
                # api_memory returns a Flask Response; extract JSON
                day = resp.get_json() if hasattr(resp, "get_json") else json.loads(resp.data)
            except Exception:
                continue
        for key in ("highlights", "tickets", "deep_reads", "people", "emails",
                    "chat_links", "documents", "search_queries"):
            items = day.get(key) or []
            # tag each with the source date so frontend can show it
            for item in items:
                if isinstance(item, dict):
                    item.setdefault("_date", d.isoformat())
            merged[key].extend(items)

    # Dedup by best key per section
    def dedup_by(lst, key_fn):
        seen = set()
        out = []
        for it in lst:
            k = key_fn(it)
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out

    merged["highlights"] = dedup_by(merged["highlights"],
                                    lambda h: (h.get("type",""), (h.get("title") or "").lower()[:60]))
    merged["tickets"] = dedup_by(merged["tickets"], lambda t: (t.get("id") or "").lower())
    merged["deep_reads"] = dedup_by(merged["deep_reads"], lambda r: (r.get("url") or "").lower()[:120])
    merged["people"] = dedup_by(merged["people"], lambda p: (p.get("name") or "").lower())
    merged["emails"] = dedup_by(merged["emails"], lambda e: (e.get("subject") or "").lower()[:80])
    merged["chat_links"] = dedup_by(merged["chat_links"], lambda c: (c.get("url") or c.get("title") or "").lower()[:120])
    merged["documents"] = dedup_by(merged["documents"], lambda d: (d.get("name") or "").lower())
    merged["search_queries"] = dedup_by(merged["search_queries"],
                                        lambda q: (q.get("query") or q.get("label") or "").lower())

    # ── LLM-enriched memory items ────────────────────────────────────
    # These come from the local_classifier daemon's memory-enrichment pass.
    # If present, they're higher quality than the regex extractors above.
    try:
        mem_rows = get_memory_items_for_window(end, days=days_back, db_path=DB_PATH)
        # Build a name → (id, weekly_summary) map so we can enrich kind=person items.
        from tracker.people import normalize_name
        person_lookup: dict[str, tuple[int, str | None]] = {}
        try:
            conn = get_connection(DB_PATH)
            for row in conn.execute(
                "SELECT id, name, canonical_name, weekly_summary FROM people"
            ).fetchall():
                entry = (row["id"], row["weekly_summary"])
                person_lookup[row["canonical_name"]] = entry
                person_lookup.setdefault(normalize_name(row["name"]), entry)
                # Also index by reversed "Last, First" form so memory_items
                # extracted from Teams (which use that form) match.
                parts = row["name"].split()
                if len(parts) >= 2:
                    last_first = f"{parts[-1]}, {' '.join(parts[:-1])}"
                    person_lookup.setdefault(normalize_name(last_first), entry)
            conn.close()
        except Exception:
            pass

        merged["memory_items"] = []
        for m in mem_rows:
            item = {
                "id": m["id"],
                "kind": m["kind"],
                "label": m["label"],
                "value": m["value"],
                "context": m["context"],
                "url": m["url"],
                "score": m["score"],
                "span_id": m["span_id"],
                "span_date": m["span_date"],
                "span_start": m["span_start"],
                "span_end": m["span_end"],
                "span_category": m["span_category"],
                "span_app": m["span_app"],
                "_date": m["span_date"],
                "time": (m["span_start"] or "")[11:16] if m["span_start"] else "",
            }
            if m["kind"] == "person" and m["value"]:
                key = normalize_name(m["value"])
                hit = person_lookup.get(key)
                if not hit and key:
                    # Substring fallback: "Vishal Vishnu" -> "Vishal Vishnu Rane"
                    for k, v in person_lookup.items():
                        if k.startswith(key + " ") or k.endswith(" " + key) or (" " + key + " ") in k:
                            hit = v
                            break
                if hit:
                    item["person_id"] = hit[0]
                    item["weekly_summary"] = hit[1]
            merged["memory_items"].append(item)
    except Exception:
        merged["memory_items"] = []

    return jsonify(merged)


@app.route("/api/hourly_breakdown")
def api_hourly_breakdown():
    """Hour-by-hour breakdown for the heatmap/bar chart."""
    init_db(DB_PATH)
    target = _parse_date()
    spans = get_spans_for_date(target, DB_PATH)
    categories = {c["name"]: c for c in get_categories(DB_PATH)}

    hours = {}
    for h in range(9, 18):
        hours[h] = {"hour": h, "categories": {}, "total_seconds": 0}

    # Collect (start, end, category) intervals clipped to each hour
    # Then merge overlapping intervals per hour, giving priority to longer spans
    hour_intervals = {h: [] for h in range(9, 18)}

    for span in spans:
        s = span.start_time
        e = span.end_time
        if e.hour < 9 or s.hour >= 18:
            continue
        cur = s
        while cur < e:
            h = cur.hour
            if h >= 18:
                break
            if h < 9:
                cur = cur.replace(hour=9, minute=0, second=0)
                continue
            hour_end = cur.replace(hour=h, minute=0, second=0) + timedelta(hours=1)
            chunk_end = min(e, hour_end)
            if chunk_end > cur:
                cat = span.category or "Unclassified"
                hour_intervals[h].append((cur, chunk_end, cat))
            cur = hour_end

    # For each hour, flatten overlapping intervals: longer spans win ties
    for h in range(9, 18):
        intervals = hour_intervals[h]
        if not intervals:
            continue
        # Sort by duration descending so longer spans get priority
        intervals.sort(key=lambda x: -(x[1] - x[0]).total_seconds())
        # Build a minute-level timeline (3600 seconds, 1-second resolution)
        hour_start = None
        for iv in intervals:
            if hour_start is None:
                hour_start = iv[0].replace(minute=0, second=0)
            break
        if hour_start is None:
            continue
        timeline = [None] * 3600  # one slot per second
        for start, end, cat in intervals:
            s_off = max(0, int((start - hour_start).total_seconds()))
            e_off = min(3600, int((end - hour_start).total_seconds()))
            for t in range(s_off, e_off):
                if timeline[t] is None:
                    timeline[t] = cat
        # Tally seconds per category
        for t in range(3600):
            if timeline[t] is not None:
                cat = timeline[t]
                if cat not in hours[h]["categories"]:
                    hours[h]["categories"][cat] = {
                        "seconds": 0,
                        "color": categories.get(cat, {}).get("color", "#7f8c8d"),
                    }
                hours[h]["categories"][cat]["seconds"] += 1
                hours[h]["total_seconds"] += 1

    return jsonify(list(hours.values()))


@app.route("/reports")
def reports():
    return render_template("reports.html")


@app.route("/search")
def search_page():
    return render_template("search.html")


@app.route("/api/people")
def api_people():
    from flask import request
    init_db(DB_PATH)
    q = request.args.get("q", "")
    if q:
        results = search_people_db(q, DB_PATH)
    else:
        results = get_people(100, DB_PATH)
    return jsonify(results)


@app.route("/api/people/<int:person_id>")
def api_person_detail(person_id):
    init_db(DB_PATH)
    conn = get_connection(DB_PATH)
    person = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
    conn.close()
    if not person:
        return jsonify({"error": "Person not found"}), 404
    activity = get_person_activity_db(person_id, 50, DB_PATH)
    return jsonify({"person": dict(person), "activity": activity})


@app.route("/api/people/regenerate-summaries", methods=["POST"])
def api_people_regenerate_summaries():
    from tracker.people_summary import generate_weekly_summaries
    init_db(DB_PATH)
    try:
        stats = generate_weekly_summaries(DB_PATH, OLLAMA_URL, OLLAMA_MODEL)
        return jsonify({"ok": True, "stats": stats})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/search")
def api_search():
    from flask import request
    init_db(DB_PATH)
    q = request.args.get("q", "")
    date_str = request.args.get("date", "")
    if not q:
        return jsonify({"spans": [], "web_pages": [], "people": []})

    d = date.fromisoformat(date_str) if date_str else None
    spans = search_spans_fts(q, 20, d, DB_PATH)
    pages = search_web_pages(q, 20, d, DB_PATH)
    people = search_people_db(q, DB_PATH)
    return jsonify({"spans": spans, "web_pages": pages, "people": people})


@app.route("/api/span_screenshots")
def api_span_screenshots():
    """Get screenshots for observation IDs belonging to a span."""
    from flask import request
    init_db(DB_PATH)
    ids_str = request.args.get("obs_ids", "")
    if not ids_str:
        return jsonify([])
    obs_ids = [int(x) for x in ids_str.split(",") if x.strip()]
    if not obs_ids:
        return jsonify([])
    conn = get_connection(DB_PATH)
    placeholders = ",".join("?" * len(obs_ids))
    rows = conn.execute(
        f"""SELECT id, timestamp, app_name, window_title, screenshot_path
            FROM observations
            WHERE id IN ({placeholders}) AND screenshot_path IS NOT NULL
            ORDER BY timestamp""",
        obs_ids,
    ).fetchall()
    conn.close()
    return jsonify([{
        "id": r["id"],
        "timestamp": r["timestamp"],
        "app": r["app_name"],
        "window": r["window_title"],
    } for r in rows])


@app.route("/api/browse")
def api_browse():
    """Get recent web pages and spans for a date (no search query needed)."""
    from flask import request
    init_db(DB_PATH)
    date_str = request.args.get("date", "")
    d = date.fromisoformat(date_str) if date_str else date.today() - timedelta(days=1)

    pages = get_web_pages_for_date(d, DB_PATH)
    spans = get_spans_for_date(d, DB_PATH)
    span_list = [{
        "id": s.id, "start_time": s.start_time.isoformat(), "end_time": s.end_time.isoformat(),
        "duration_seconds": s.duration_seconds, "app_name": s.app_name,
        "category": s.category, "subcategory": s.subcategory,
        "description": s.description, "window_title": s.window_title,
    } for s in spans[:20]]

    return jsonify({"web_pages": pages, "spans": span_list})


@app.route("/api/weekly_trends")
def api_weekly_trends():
    """Category breakdown per day for the last N weeks."""
    from flask import request
    weeks = int(request.args.get("weeks", 2))
    init_db(DB_PATH)
    categories = {c["name"]: c for c in get_categories(DB_PATH)}

    today = date.today()
    # Start from Monday of (weeks) weeks ago
    start = today - timedelta(days=today.weekday() + (weeks - 1) * 7)
    days = []

    for i in range((today - start).days + 1):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:  # skip weekends
            continue
        spans = get_spans_for_date(d, DB_PATH)
        work_spans = [s for s in spans if 9 <= s.start_time.hour < 18]

        cat_totals = {}
        for s in work_spans:
            cat = s.category or "Unclassified"
            cat_totals[cat] = cat_totals.get(cat, 0) + s.duration_seconds

        # Focus score — purposeful work includes meetings (CSM role)
        purposeful_cats = {"Development", "Research", "Documentation", "Deep Work",
                           "Meetings", "Meetings/Calls", "Planning", "Communication"}
        distraction_cats = {"Personal", "Entertainment", "Break"}
        total = sum(cat_totals.values())
        purposeful = sum(v for k, v in cat_totals.items() if k in purposeful_cats)
        distraction = sum(v for k, v in cat_totals.items() if k in distraction_cats)

        switches = 0
        sorted_spans = sorted(work_spans, key=lambda s: s.start_time)
        for j in range(1, len(sorted_spans)):
            if (sorted_spans[j].category or "") != (sorted_spans[j-1].category or ""):
                switches += 1

        focus = 0
        if total > 0:
            purposeful_ratio = purposeful / total
            distraction_ratio = distraction / total
            switch_score = max(0, 1 - switches / 40)
            focus = round(purposeful_ratio * 40 + switch_score * 30 + (1 - distraction_ratio) * 30)

        wall = _compute_wall_clock([s for s in work_spans
                                    if (s.category or "") not in {"Idle/AFK", "Break", "Idle"}])

        days.append({
            "date": d.isoformat(),
            "day_name": d.strftime("%a"),
            "date_display": d.strftime("%d %b"),
            "total_seconds": wall,
            "focus_score": focus,
            "categories": {k: {"seconds": v, "color": categories.get(k, {}).get("color", "#7f8c8d")}
                           for k, v in cat_totals.items()},
            "switches": switches,
        })

    # Build comparison: this week vs last week
    this_week = [d for d in days if date.fromisoformat(d["date"]).isocalendar()[1] == today.isocalendar()[1]]
    last_week = [d for d in days if date.fromisoformat(d["date"]).isocalendar()[1] == today.isocalendar()[1] - 1]

    def week_summary(week_days):
        total = sum(d["total_seconds"] for d in week_days)
        avg_focus = round(sum(d["focus_score"] for d in week_days) / len(week_days)) if week_days else 0
        avg_switches = round(sum(d["switches"] for d in week_days) / len(week_days)) if week_days else 0
        cat_totals = {}
        for d in week_days:
            for cat, info in d["categories"].items():
                if cat not in cat_totals:
                    cat_totals[cat] = {"seconds": 0, "color": info["color"]}
                cat_totals[cat]["seconds"] += info["seconds"]
        return {
            "total_seconds": total,
            "avg_focus": avg_focus,
            "avg_switches": avg_switches,
            "days_tracked": len(week_days),
            "categories": cat_totals,
        }

    return jsonify({
        "days": days,
        "this_week": week_summary(this_week),
        "last_week": week_summary(last_week),
        "all_categories": {c["name"]: {"color": c.get("color", "#7f8c8d")}
                           for c in get_categories(DB_PATH)},
    })


_AI = ai_settings()
OLLAMA_URL = _AI["ollama_url"]
OLLAMA_MODEL = _AI["model"]

# Cache digests to avoid regenerating on every page refresh
_digest_cache: dict[str, dict] = {}


@app.route("/api/digest")
def api_digest():
    """Generate an AI-powered daily digest using local Ollama."""
    import requests as req

    init_db(DB_PATH)
    target = _parse_date()
    cache_key = target.isoformat()

    # Return cached digest if fresh (< 15 min old)
    if cache_key in _digest_cache:
        cached = _digest_cache[cache_key]
        age = (datetime.now() - cached["generated_at"]).total_seconds()
        if age < 900:
            return jsonify(cached["data"])

    spans = get_spans_for_date(target, DB_PATH)
    work_spans = [s for s in spans if 9 <= s.start_time.hour < 18]

    if not work_spans:
        return jsonify({"digest": "No classified activity for this day yet.", "generated": False})

    # Build a concise context for the LLM
    cat_totals = defaultdict(int)
    timeline_items = []
    people_mentioned = set()
    tickets_mentioned = set()

    ticket_re = re.compile(r'(CS\d{10,}|SR\s*\d{6,}|INC\d{6,}|[A-Z]+-\d{3,})', re.IGNORECASE)

    for s in sorted(work_spans, key=lambda x: x.start_time):
        cat = s.category or "Unclassified"
        cat_totals[cat] += s.duration_seconds
        desc = s.description or s.window_title or s.app_name
        dur_min = s.duration_seconds // 60
        if dur_min >= 3:  # skip tiny spans
            timeline_items.append(f"  {s.start_time.strftime('%H:%M')}-{s.end_time.strftime('%H:%M')} ({dur_min}m) [{cat}] {desc}")

        # Extract people and tickets from descriptions
        for m in ticket_re.finditer(desc or ""):
            tickets_mentioned.add(m.group(1))

    # Category summary
    cat_lines = []
    for cat, secs in sorted(cat_totals.items(), key=lambda x: -x[1]):
        h, m = secs // 3600, (secs % 3600) // 60
        cat_lines.append(f"  {cat}: {h}h {m}m")

    prompt = f"""You are a work activity analyst. Summarise this person's workday concisely.

Date: {target.strftime('%A, %d %B %Y')}

Time by category:
{chr(10).join(cat_lines)}

Timeline (work hours only):
{chr(10).join(timeline_items[:30])}

Tickets referenced: {', '.join(tickets_mentioned) if tickets_mentioned else 'None'}

Write a 3-5 sentence natural language summary of the day. Be specific — mention actual activities, people, tickets, and tools by name. Note any patterns (heavy meeting day, deep focus afternoon, lots of context switching). End with one actionable observation. Do not use bullet points. Do not repeat the raw data. Write as if you're a helpful colleague giving a quick recap."""

    try:
        resp = req.post(
            f"{OLLAMA_URL}/api/chat",
            json={"model": OLLAMA_MODEL, "stream": False, "think": False,
                  "messages": [{"role": "user", "content": prompt}],
                  "options": {"temperature": 0.4, "num_predict": 300}},
            timeout=60,
        )
        resp.raise_for_status()
        digest_text = resp.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        return jsonify({"digest": f"Ollama unavailable: {e}", "generated": False})

    result = {
        "digest": digest_text,
        "generated": True,
        "model": OLLAMA_MODEL,
        "date": target.isoformat(),
    }
    _digest_cache[cache_key] = {"data": result, "generated_at": datetime.now()}
    return jsonify(result)


def _compute_wall_clock(spans) -> int:
    """Compute wall-clock seconds from potentially overlapping spans."""
    if not spans:
        return 0
    # Merge overlapping intervals
    intervals = sorted([(s.start_time, s.end_time) for s in spans])
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum(int((e - s).total_seconds()) for s, e in merged)


def _parse_date():
    from flask import request
    date_str = request.args.get("date", "")
    if date_str:
        return date.fromisoformat(date_str)
    return date.today()


if __name__ == "__main__":
    init_db(DB_PATH)
    app.run(host="127.0.0.1", port=8420, debug=True)
