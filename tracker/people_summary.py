"""Precomputed weekly conversation summaries per person.

For each person with interactions in the last 7 days, ask Ollama to write a
short summary of what was discussed, and stash it on the people row.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta

import requests

from tracker.db import DEFAULT_DB_PATH, get_connection

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

WINDOW_DAYS = 7
MAX_ROWS_PER_PERSON = 60


def _build_prompt(name: str, rows: list[dict]) -> str:
    by_day: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        ts = r.get("timestamp") or ""
        day = ts.split("T")[0] if "T" in ts else ts[:10]
        bits = []
        itype = r.get("interaction_type")
        if itype:
            bits.append(f"[{itype}]")
        cat = r.get("category")
        if cat:
            bits.append(f"({cat})")
        ctx = r.get("span_description") or r.get("context") or r.get("window_title") or ""
        ctx = ctx.strip().replace("\n", " ")
        if len(ctx) > 220:
            ctx = ctx[:220] + "…"
        if ctx:
            bits.append(ctx)
        if bits:
            by_day[day].append(" ".join(bits))

    day_lines = []
    for day in sorted(by_day.keys()):
        items = by_day[day][:8]
        day_lines.append(f"{day}:")
        for it in items:
            day_lines.append(f"  - {it}")

    return f"""You are summarising what someone discussed with a specific colleague over the last week.

Colleague: {name}

Interactions (chats, meetings, emails) over the last 7 days:
{chr(10).join(day_lines)}

Write 2-3 sentences (under 80 words) describing what was discussed with {name} this week. Be specific — name actual topics, tickets, or projects when you can see them. If the data is thin or only shows generic chat windows with no topic, say "Brief contact this week, no clear topic." Do not use bullet points. Do not invent details that aren't in the data."""


def _fetch_recent_activity(conn, person_id: int, since_iso: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT pa.timestamp, pa.interaction_type, pa.context,
               s.description AS span_description,
               s.window_title AS window_title,
               s.category AS category
          FROM person_activity pa
          LEFT JOIN activity_spans s ON s.id = pa.span_id
         WHERE pa.person_id = ?
           AND pa.timestamp >= ?
         ORDER BY pa.timestamp ASC
         LIMIT ?
        """,
        (person_id, since_iso, MAX_ROWS_PER_PERSON),
    ).fetchall()
    return [dict(r) for r in rows]


def _call_ollama(prompt: str, ollama_url: str, model: str) -> str:
    resp = requests.post(
        f"{ollama_url}/api/chat",
        json={
            "model": model,
            "stream": False,
            "think": False,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0.3, "num_predict": 200},
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("message", {}).get("content", "").strip()


def generate_weekly_summaries(
    db_path: str = DEFAULT_DB_PATH,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Regenerate weekly_summary for every person with recent activity.

    People with no interactions in the last 7 days have their summary cleared,
    so stale ones don't linger in the UI.
    """
    now = datetime.now()
    since = now - timedelta(days=WINDOW_DAYS)
    since_iso = since.isoformat()
    generated_at = now.isoformat()

    conn = get_connection(db_path)
    try:
        active_ids = {
            row["person_id"]
            for row in conn.execute(
                "SELECT DISTINCT person_id FROM person_activity WHERE timestamp >= ?",
                (since_iso,),
            ).fetchall()
        }

        all_people = conn.execute("SELECT id, name FROM people").fetchall()
        stats = {"updated": 0, "cleared": 0, "skipped": 0, "errors": 0}

        for person in all_people:
            pid = person["id"]
            name = person["name"]
            if pid not in active_ids:
                conn.execute(
                    "UPDATE people SET weekly_summary = NULL, weekly_summary_generated_at = ? WHERE id = ?",
                    (generated_at, pid),
                )
                stats["cleared"] += 1
                continue

            rows = _fetch_recent_activity(conn, pid, since_iso)
            if not rows:
                stats["skipped"] += 1
                continue

            prompt = _build_prompt(name, rows)
            try:
                summary = _call_ollama(prompt, ollama_url, model)
            except Exception as exc:
                logger.warning("Ollama failed for person %s (%s): %s", pid, name, exc)
                stats["errors"] += 1
                continue

            if not summary:
                stats["skipped"] += 1
                continue

            conn.execute(
                "UPDATE people SET weekly_summary = ?, weekly_summary_generated_at = ? WHERE id = ?",
                (summary, generated_at, pid),
            )
            stats["updated"] += 1

        conn.commit()
        return stats
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stats = generate_weekly_summaries()
    logger.info("People weekly summaries: %s", stats)


if __name__ == "__main__":
    main()
