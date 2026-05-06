import sqlite3
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from tracker.models import Observation, ActivitySpan

DEFAULT_DB_PATH = os.path.expanduser("~/.local/share/commonplace/activity.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    app_name TEXT NOT NULL,
    window_title TEXT,
    browser_url TEXT,
    browser_tab_title TEXT,
    is_idle INTEGER DEFAULT 0,
    screenshot_path TEXT,
    classified INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS activity_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    app_name TEXT NOT NULL,
    window_title TEXT,
    browser_url TEXT,
    category TEXT,
    subcategory TEXT,
    description TEXT,
    observation_count INTEGER DEFAULT 1,
    observation_ids TEXT
);

CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    color TEXT,
    is_productive INTEGER
);

CREATE INDEX IF NOT EXISTS idx_obs_timestamp ON observations(timestamp);
CREATE INDEX IF NOT EXISTS idx_obs_classified ON observations(classified);
CREATE INDEX IF NOT EXISTS idx_spans_start ON activity_spans(start_time);
CREATE INDEX IF NOT EXISTS idx_spans_category ON activity_spans(category);

-- LLM-enriched memory items (populated by local_classifier daemon)
CREATE TABLE IF NOT EXISTS memory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- 'ticket' | 'person' | 'doc' | 'link' | 'snippet' | 'pinned'
    label TEXT,                  -- short kind-label shown to user (e.g., "Jira", "Customer")
    value TEXT NOT NULL,         -- the main text — ticket ID, person name, doc name, snippet text
    context TEXT,                -- 1-sentence LLM-written description of why it matters
    url TEXT,                    -- optional deep link
    score INTEGER DEFAULT 5,     -- 0-10 LLM-judged importance
    created_at TEXT NOT NULL,
    span_date TEXT NOT NULL,     -- YYYY-MM-DD of the parent span for quick window queries
    FOREIGN KEY (span_id) REFERENCES activity_spans(id)
);
CREATE INDEX IF NOT EXISTS idx_memory_span ON memory_items(span_id);
CREATE INDEX IF NOT EXISTS idx_memory_date ON memory_items(span_date);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_items(kind);

-- Web page content
CREATE TABLE IF NOT EXISTS web_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    title TEXT,
    content TEXT,
    content_length INTEGER DEFAULT 0,
    captured_at TEXT NOT NULL,
    observation_id INTEGER,
    FOREIGN KEY (observation_id) REFERENCES observations(id)
);
CREATE INDEX IF NOT EXISTS idx_web_pages_url ON web_pages(url);
CREATE INDEX IF NOT EXISTS idx_web_pages_captured ON web_pages(captured_at);

-- People hub
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    email TEXT,
    organization TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    interaction_count INTEGER DEFAULT 1,
    notes TEXT,
    weekly_summary TEXT,
    weekly_summary_generated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_people_canonical ON people(canonical_name);

CREATE TABLE IF NOT EXISTS person_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    span_id INTEGER,
    observation_id INTEGER,
    interaction_type TEXT,
    context TEXT,
    timestamp TEXT NOT NULL,
    FOREIGN KEY (person_id) REFERENCES people(id),
    FOREIGN KEY (span_id) REFERENCES activity_spans(id),
    FOREIGN KEY (observation_id) REFERENCES observations(id)
);
CREATE INDEX IF NOT EXISTS idx_person_activity_person ON person_activity(person_id);
CREATE INDEX IF NOT EXISTS idx_person_activity_ts ON person_activity(timestamp);

-- Links hub: every unique URL observed, with aggregated context
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    hostname TEXT,
    title TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    visit_count INTEGER DEFAULT 1,
    total_dwell_seconds REAL DEFAULT 0,
    contexts TEXT DEFAULT '[]',
    source TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_links_url ON links(url);
CREATE INDEX IF NOT EXISTS idx_links_hostname ON links(hostname);
CREATE INDEX IF NOT EXISTS idx_links_last_seen ON links(last_seen);
"""

DEFAULT_CATEGORIES = [
    # Canonical categories — warm palette that harmonizes with peach/cream bg
    ("Development", "#0f766e", 1),       # muted teal — cool anchor for focused work
    ("Communication", "#385e81", None),  # dusty slate blue — cool but muted, pairs with warm bg
    ("Meetings", "#9a3412", 1),          # rust — grounded, grown-up
    ("Research", "#b45309", 1),          # amber — curiosity
    ("Planning", "#d97706", 1),          # amber-dark — forward-leaning
    ("Documentation", "#78350f", 1),     # chocolate — solid, written
    ("Admin", "#a16207", None),          # dark gold — routine
    ("Personal", "#991b1b", 0),          # brick red — signals non-work
    ("Entertainment", "#7c2d12", 0),     # deep rust — dusky, leisure
    ("Break", "#d4b896", None),          # warm beige — breath, not void
    # Legacy aliases — mapped to canonical colors
    ("Meetings/Calls", "#9a3412", 1),
    ("Email", "#d97706", 1),
    ("Slides/Presentations", "#78350f", 1),
    ("Code/Development", "#0f766e", 1),
    ("Documents", "#78350f", 1),
    ("Browsing/Research", "#b45309", None),
    ("Idle/AFK", "#d4b896", None),
    ("System/Other", "#8b7355", None),
    ("AI/Productivity", "#385e81", 1),
    ("Deep Work", "#0f766e", 1),
    ("Browsing", "#b45309", None),
]


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    # Migrate: add screenshot_path column if missing (for existing DBs)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(observations)").fetchall()]
    if "screenshot_path" not in columns:
        conn.execute("ALTER TABLE observations ADD COLUMN screenshot_path TEXT")
        conn.commit()
    people_columns = [row[1] for row in conn.execute("PRAGMA table_info(people)").fetchall()]
    if "weekly_summary" not in people_columns:
        conn.execute("ALTER TABLE people ADD COLUMN weekly_summary TEXT")
    if "weekly_summary_generated_at" not in people_columns:
        conn.execute("ALTER TABLE people ADD COLUMN weekly_summary_generated_at TEXT")
    conn.commit()
    for name, color, productive in DEFAULT_CATEGORIES:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, color, is_productive) VALUES (?, ?, ?)",
            (name, color, productive),
        )
    # FTS5 virtual tables (created separately — can't use IF NOT EXISTS in executescript)
    _init_fts(conn)
    conn.commit()
    conn.close()


def _init_fts(conn: sqlite3.Connection) -> None:
    """Create FTS5 virtual tables and sync triggers if they don't exist."""
    existing = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
    ).fetchall()}

    if "web_pages_fts" not in existing:
        conn.execute("""
            CREATE VIRTUAL TABLE web_pages_fts USING fts5(
                url, title, content, content=web_pages, content_rowid=id
            )
        """)
    if "web_pages_fts_ai" not in existing:
        conn.execute("""
            CREATE TRIGGER web_pages_fts_ai AFTER INSERT ON web_pages BEGIN
                INSERT INTO web_pages_fts(rowid, url, title, content)
                VALUES (new.id, new.url, new.title, new.content);
            END
        """)

    if "spans_fts" not in existing:
        conn.execute("""
            CREATE VIRTUAL TABLE spans_fts USING fts5(
                app_name, window_title, browser_url, category, subcategory, description,
                content=activity_spans, content_rowid=id
            )
        """)
    if "spans_fts_ai" not in existing:
        conn.execute("""
            CREATE TRIGGER spans_fts_ai AFTER INSERT ON activity_spans BEGIN
                INSERT INTO spans_fts(rowid, app_name, window_title, browser_url,
                    category, subcategory, description)
                VALUES (new.id, new.app_name, new.window_title, new.browser_url,
                    new.category, new.subcategory, new.description);
            END
        """)


def insert_observation(obs: Observation, db_path: str = DEFAULT_DB_PATH) -> int:
    conn = get_connection(db_path)
    cursor = conn.execute(
        """INSERT INTO observations (timestamp, app_name, window_title, browser_url,
           browser_tab_title, is_idle, screenshot_path, classified)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            obs.timestamp.isoformat(),
            obs.app_name,
            obs.window_title,
            obs.browser_url,
            obs.browser_tab_title,
            1 if obs.is_idle else 0,
            obs.screenshot_path,
            1 if obs.classified else 0,
        ),
    )
    obs_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return obs_id


def get_unclassified_observations(db_path: str = DEFAULT_DB_PATH) -> list[Observation]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM observations WHERE classified = 0 ORDER BY timestamp"
    ).fetchall()
    conn.close()
    return [_row_to_observation(r) for r in rows]


def get_observations_for_date(
    target_date: date, db_path: str = DEFAULT_DB_PATH
) -> list[Observation]:
    conn = get_connection(db_path)
    date_str = target_date.isoformat()
    rows = conn.execute(
        "SELECT * FROM observations WHERE timestamp >= ? AND timestamp < date(?, '+1 day') ORDER BY timestamp",
        (date_str, date_str),
    ).fetchall()
    conn.close()
    return [_row_to_observation(r) for r in rows]


def get_recent_observations(
    minutes: int = 30, db_path: str = DEFAULT_DB_PATH
) -> list[Observation]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM observations WHERE timestamp >= datetime('now', 'localtime', ?) ORDER BY timestamp",
        (f"-{minutes} minutes",),
    ).fetchall()
    conn.close()
    return [_row_to_observation(r) for r in rows]


def insert_activity_span(span: ActivitySpan, db_path: str = DEFAULT_DB_PATH) -> int:
    conn = get_connection(db_path)
    obs_ids_str = ",".join(str(i) for i in span.observation_ids) if span.observation_ids else None
    cursor = conn.execute(
        """INSERT INTO activity_spans (start_time, end_time, duration_seconds, app_name,
           window_title, browser_url, category, subcategory, description,
           observation_count, observation_ids)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            span.start_time.isoformat(),
            span.end_time.isoformat(),
            span.duration_seconds,
            span.app_name,
            span.window_title,
            span.browser_url,
            span.category,
            span.subcategory,
            span.description,
            span.observation_count,
            obs_ids_str,
        ),
    )
    span_id = cursor.lastrowid
    # Mark observations as classified
    if span.observation_ids:
        placeholders = ",".join("?" * len(span.observation_ids))
        conn.execute(
            f"UPDATE observations SET classified = 1 WHERE id IN ({placeholders})",
            span.observation_ids,
        )
    conn.commit()
    conn.close()
    return span_id


def get_spans_for_date(
    target_date: date, db_path: str = DEFAULT_DB_PATH
) -> list[ActivitySpan]:
    conn = get_connection(db_path)
    date_str = target_date.isoformat()
    rows = conn.execute(
        "SELECT * FROM activity_spans WHERE start_time >= ? AND start_time < date(?, '+1 day') ORDER BY start_time",
        (date_str, date_str),
    ).fetchall()
    conn.close()
    return [_row_to_span(r) for r in rows]


def get_category_breakdown(
    target_date: date, db_path: str = DEFAULT_DB_PATH
) -> list[dict]:
    conn = get_connection(db_path)
    date_str = target_date.isoformat()
    rows = conn.execute(
        """SELECT category, SUM(duration_seconds) as total_seconds, COUNT(*) as span_count
           FROM activity_spans
           WHERE start_time >= ? AND start_time < date(?, '+1 day')
           GROUP BY category
           ORDER BY total_seconds DESC""",
        (date_str, date_str),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_categories(db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_category(
    name: str, color: str = "#7f8c8d", is_productive: Optional[int] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    conn = get_connection(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO categories (name, color, is_productive) VALUES (?, ?, ?)",
        (name, color, is_productive),
    )
    conn.commit()
    conn.close()


def get_unclassified_spans(db_path: str = DEFAULT_DB_PATH) -> list[ActivitySpan]:
    """Get observations grouped into spans that haven't been classified yet."""
    observations = get_unclassified_observations(db_path)
    # Filter out idle observations — they shouldn't become classified spans
    active_obs = [o for o in observations if not o.is_idle]
    # Mark idle observations as classified so they don't pile up
    idle_ids = [o.id for o in observations if o.is_idle and o.id is not None]
    if idle_ids:
        conn = get_connection(db_path)
        conn.executemany(
            "UPDATE observations SET classified = 1 WHERE id = ?",
            [(i,) for i in idle_ids],
        )
        conn.commit()
        conn.close()
    return group_observations_into_spans(active_obs)


def group_observations_into_spans(observations: list[Observation], gap_seconds: int = 300) -> list[ActivitySpan]:
    """Group consecutive observations with the same app into spans.

    Observations of the same app are merged even if window titles differ,
    as long as there's no gap longer than gap_seconds (default 5 min).
    """
    if not observations:
        return []

    spans = []
    current_obs = [observations[0]]

    for obs in observations[1:]:
        prev = current_obs[-1]
        same_app = obs.app_name == prev.app_name
        time_gap = (obs.timestamp - prev.timestamp).total_seconds()
        # Same app within the gap window stays grouped
        if same_app and time_gap <= gap_seconds:
            current_obs.append(obs)
        else:
            spans.append(_observations_to_span(current_obs))
            current_obs = [obs]

    spans.append(_observations_to_span(current_obs))
    return spans


def _observations_to_span(observations: list[Observation]) -> ActivitySpan:
    first = observations[0]
    last = observations[-1]
    duration = int((last.timestamp - first.timestamp).total_seconds())
    # Minimum duration is the poll interval (assume 5s for single observations)
    if duration == 0:
        duration = 5

    # Pick the most common window title
    titles = [o.window_title for o in observations if o.window_title]
    window_title = max(set(titles), key=titles.count) if titles else None

    # Pick the most common URL
    urls = [o.browser_url for o in observations if o.browser_url]
    browser_url = max(set(urls), key=urls.count) if urls else None

    return ActivitySpan(
        start_time=first.timestamp,
        end_time=last.timestamp,
        duration_seconds=duration,
        app_name=first.app_name,
        window_title=window_title,
        browser_url=browser_url,
        observation_count=len(observations),
        observation_ids=[o.id for o in observations if o.id is not None],
    )


def _row_to_observation(row: sqlite3.Row) -> Observation:
    # Handle both old schema (no screenshot_path) and new schema
    screenshot_path = None
    try:
        screenshot_path = row["screenshot_path"]
    except (IndexError, KeyError):
        pass
    return Observation(
        id=row["id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        app_name=row["app_name"],
        window_title=row["window_title"],
        browser_url=row["browser_url"],
        browser_tab_title=row["browser_tab_title"],
        is_idle=bool(row["is_idle"]),
        screenshot_path=screenshot_path,
        classified=bool(row["classified"]),
    )


def _row_to_span(row: sqlite3.Row) -> ActivitySpan:
    obs_ids_str = row["observation_ids"]
    obs_ids = [int(x) for x in obs_ids_str.split(",")] if obs_ids_str else []
    return ActivitySpan(
        id=row["id"],
        start_time=datetime.fromisoformat(row["start_time"]),
        end_time=datetime.fromisoformat(row["end_time"]),
        duration_seconds=row["duration_seconds"],
        app_name=row["app_name"],
        window_title=row["window_title"],
        browser_url=row["browser_url"],
        category=row["category"],
        subcategory=row["subcategory"],
        description=row["description"],
        observation_count=row["observation_count"],
        observation_ids=obs_ids,
    )


# ── Web Pages ──────────────────────────────────────────────────────────

def insert_web_page(
    url: str, title: Optional[str], content: str,
    observation_id: Optional[int] = None, db_path: str = DEFAULT_DB_PATH,
) -> int:
    conn = get_connection(db_path)
    cursor = conn.execute(
        """INSERT INTO web_pages (url, title, content, content_length, captured_at, observation_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (url, title, content, len(content), datetime.now().isoformat(), observation_id),
    )
    page_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return page_id


def was_recently_fetched(url: str, hours: int = 24, db_path: str = DEFAULT_DB_PATH) -> bool:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT 1 FROM web_pages WHERE url = ? AND captured_at >= datetime('now', 'localtime', ?) LIMIT 1",
        (url, f"-{hours} hours"),
    ).fetchone()
    conn.close()
    return row is not None


def search_web_pages(query: str, limit: int = 20, target_date: Optional[date] = None,
                     db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    if target_date:
        date_str = target_date.isoformat()
        rows = conn.execute(
            """SELECT wp.id, wp.url, wp.title, snippet(web_pages_fts, 2, '<b>', '</b>', '...', 40) as snippet,
                      wp.captured_at
               FROM web_pages_fts fts
               JOIN web_pages wp ON wp.id = fts.rowid
               WHERE web_pages_fts MATCH ? AND wp.captured_at >= ? AND wp.captured_at < date(?, '+1 day')
               ORDER BY rank LIMIT ?""",
            (query, date_str, date_str, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT wp.id, wp.url, wp.title, snippet(web_pages_fts, 2, '<b>', '</b>', '...', 40) as snippet,
                      wp.captured_at
               FROM web_pages_fts fts
               JOIN web_pages wp ON wp.id = fts.rowid
               WHERE web_pages_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_web_pages_for_date(target_date: date, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    date_str = target_date.isoformat()
    rows = conn.execute(
        """SELECT id, url, title, substr(content, 1, 200) as content_preview, content_length, captured_at
           FROM web_pages
           WHERE captured_at >= ? AND captured_at < date(?, '+1 day')
           ORDER BY captured_at""",
        (date_str, date_str),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── People ─────────────────────────────────────────────────────────────

def get_or_create_person(name: str, canonical_name: str, timestamp: str,
                         db_path: str = DEFAULT_DB_PATH) -> int:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT id FROM people WHERE canonical_name = ?", (canonical_name,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE people SET last_seen = ?, interaction_count = interaction_count + 1 WHERE id = ?",
            (timestamp, row["id"]),
        )
        conn.commit()
        person_id = row["id"]
    else:
        cursor = conn.execute(
            "INSERT INTO people (name, canonical_name, first_seen, last_seen) VALUES (?, ?, ?, ?)",
            (name, canonical_name, timestamp, timestamp),
        )
        person_id = cursor.lastrowid
        conn.commit()
    conn.close()
    return person_id


def insert_person_activity(
    person_id: int, interaction_type: str, context: str, timestamp: str,
    span_id: Optional[int] = None, observation_id: Optional[int] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    conn = get_connection(db_path)
    conn.execute(
        """INSERT INTO person_activity (person_id, span_id, observation_id, interaction_type, context, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (person_id, span_id, observation_id, interaction_type, context, timestamp),
    )
    conn.commit()
    conn.close()


def get_people(limit: int = 50, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM people ORDER BY interaction_count DESC, last_seen DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_people_db(query: str, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM people WHERE name LIKE ? OR canonical_name LIKE ? ORDER BY interaction_count DESC LIMIT 20",
        (f"%{query}%", f"%{query}%"),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_person_activity_db(person_id: int, limit: int = 30,
                           db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT pa.*, s.category, s.description as span_description, s.start_time, s.end_time
           FROM person_activity pa
           LEFT JOIN activity_spans s ON s.id = pa.span_id
           WHERE pa.person_id = ?
           ORDER BY pa.timestamp DESC LIMIT ?""",
        (person_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Full-text search across spans ──────────────────────────────────────

def search_spans_fts(query: str, limit: int = 20, target_date: Optional[date] = None,
                     db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    if target_date:
        date_str = target_date.isoformat()
        rows = conn.execute(
            """SELECT s.id, s.start_time, s.end_time, s.duration_seconds, s.app_name,
                      s.category, s.subcategory, s.description, s.window_title,
                      snippet(spans_fts, 5, '<b>', '</b>', '...', 40) as snippet
               FROM spans_fts fts
               JOIN activity_spans s ON s.id = fts.rowid
               WHERE spans_fts MATCH ? AND s.start_time >= ? AND s.start_time < date(?, '+1 day')
               ORDER BY rank LIMIT ?""",
            (query, date_str, date_str, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.id, s.start_time, s.end_time, s.duration_seconds, s.app_name,
                      s.category, s.subcategory, s.description, s.window_title,
                      snippet(spans_fts, 5, '<b>', '</b>', '...', 40) as snippet
               FROM spans_fts fts
               JOIN activity_spans s ON s.id = fts.rowid
               WHERE spans_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_link(url: str, title: str | None, timestamp: datetime,
                context: str | None = None, source: str | None = None,
                dwell_seconds: float = 5.0, db_path: str = DEFAULT_DB_PATH) -> None:
    """Insert or update a link in the links table."""
    from urllib.parse import urlparse
    import json

    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    # Skip non-http, empty, and internal URLs
    if not url or not url.startswith("http"):
        return
    if hostname in ("", "localhost", "127.0.0.1", "newtab"):
        return

    ts = timestamp.isoformat()
    conn = get_connection(db_path)

    existing = conn.execute("SELECT id, contexts, visit_count, total_dwell_seconds FROM links WHERE url = ?", (url,)).fetchone()
    if existing:
        # Update existing
        contexts = json.loads(existing["contexts"] or "[]")
        if context and context not in contexts:
            contexts.append(context)
            contexts = contexts[-5:]  # keep last 5 contexts
        conn.execute(
            """UPDATE links SET last_seen = ?, visit_count = visit_count + 1,
               total_dwell_seconds = total_dwell_seconds + ?, title = COALESCE(?, title),
               contexts = ? WHERE id = ?""",
            (ts, dwell_seconds, title, json.dumps(contexts), existing["id"]),
        )
    else:
        contexts = [context] if context else []
        conn.execute(
            """INSERT INTO links (url, hostname, title, first_seen, last_seen,
               visit_count, total_dwell_seconds, contexts, source)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (url, hostname, title, ts, ts, dwell_seconds, json.dumps(contexts), source),
        )
    conn.commit()
    conn.close()


def search_links(query: str, limit: int = 20, target_date: date | None = None,
                 db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    """Search links by URL, title, hostname, or context."""
    conn = get_connection(db_path)
    q = f"%{query}%"
    if target_date:
        rows = conn.execute(
            """SELECT * FROM links
               WHERE (url LIKE ? OR title LIKE ? OR hostname LIKE ? OR contexts LIKE ?)
               AND date(last_seen) = ?
               ORDER BY total_dwell_seconds DESC LIMIT ?""",
            (q, q, q, q, target_date.isoformat(), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM links
               WHERE url LIKE ? OR title LIKE ? OR hostname LIKE ? OR contexts LIKE ?
               ORDER BY last_seen DESC LIMIT ?""",
            (q, q, q, q, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_links_for_date(target: date, min_dwell: float = 0, db_path: str = DEFAULT_DB_PATH) -> list[dict]:
    """Get all links visited on a specific date, ordered by dwell time."""
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT * FROM links
           WHERE date(first_seen) <= ? AND date(last_seen) >= ?
           AND total_dwell_seconds >= ?
           ORDER BY total_dwell_seconds DESC""",
        (target.isoformat(), target.isoformat(), min_dwell),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def insert_memory_item(
    span_id: int,
    kind: str,
    value: str,
    label: str = None,
    context: str = None,
    url: str = None,
    score: int = 5,
    span_date: str = None,
    db_path: str = DEFAULT_DB_PATH,
) -> int:
    """Insert an LLM-enriched memory item tied to a classified span."""
    conn = get_connection(db_path)
    cur = conn.execute(
        """INSERT INTO memory_items
           (span_id, kind, label, value, context, url, score, created_at, span_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (span_id, kind, label, value, context, url, score,
         datetime.now().isoformat(), span_date or date.today().isoformat()),
    )
    conn.commit()
    item_id = cur.lastrowid
    conn.close()
    return item_id


def get_memory_items_for_window(
    end_date: date, days: int = 10, min_score: int = 0, db_path: str = DEFAULT_DB_PATH
) -> list[dict]:
    """Get LLM-enriched memory items across a rolling window of days."""
    start = end_date - timedelta(days=days - 1)
    conn = get_connection(db_path)
    rows = conn.execute(
        """SELECT m.*, s.start_time AS span_start, s.end_time AS span_end,
                  s.category AS span_category, s.app_name AS span_app, s.description AS span_description
           FROM memory_items m
           JOIN activity_spans s ON s.id = m.span_id
           WHERE m.span_date >= ? AND m.span_date <= ?
             AND m.score >= ?
           ORDER BY m.score DESC, m.created_at DESC""",
        (start.isoformat(), end_date.isoformat(), min_score),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def has_memory_items_for_span(span_id: int, db_path: str = DEFAULT_DB_PATH) -> bool:
    """Check if memory items already exist for this span (idempotency guard)."""
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT COUNT(*) as c FROM memory_items WHERE span_id = ?", (span_id,)
    ).fetchone()
    conn.close()
    return row["c"] > 0


def backfill_spans_fts(db_path: str = DEFAULT_DB_PATH) -> int:
    """Populate spans_fts with existing activity_spans not yet indexed."""
    conn = get_connection(db_path)
    count = conn.execute(
        """INSERT INTO spans_fts(rowid, app_name, window_title, browser_url, category, subcategory, description)
           SELECT id, app_name, window_title, browser_url, category, subcategory, description
           FROM activity_spans
           WHERE id NOT IN (SELECT rowid FROM spans_fts)"""
    ).rowcount
    conn.commit()
    conn.close()
    return count
