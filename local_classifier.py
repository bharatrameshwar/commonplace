#!/usr/bin/env python3
"""Local activity classifier using Ollama (Qwen3 8B).

Runs as a daemon, classifying unclassified activity spans every few minutes.
Calls the same DB functions as the MCP server — no MCP round-trips needed.
"""

import json
import logging
import time
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from tracker.config import ai_settings, user_name, user_organization, user_role
from tracker.db import (
    DEFAULT_DB_PATH,
    get_unclassified_spans,
    insert_activity_span,
    get_connection,
    init_db,
    get_categories,
    insert_memory_item,
    has_memory_items_for_span,
)
from tracker.models import ActivitySpan
from tracker.people import extract_and_link_people

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_AI = ai_settings()
OLLAMA_MODEL = _AI["model"]
OLLAMA_URL = _AI["ollama_url"].rstrip("/") + "/api/chat"
BATCH_SIZE = 20  # spans per LLM call
INTERVAL_SECONDS = 300  # classify every 5 minutes

CATEGORIES = [
    "Development",
    "Communication",
    "Meetings",
    "Documentation",
    "Planning",
    "Research",
    "Creative",
    "Personal",
    "Admin",
    "Break",
]

def _user_context_line() -> str:
    """One sentence describing the user. Drives prompt grounding.

    Reads from config.yaml `user_profile`. Empty string if not configured —
    the classifier still works, just without role/org context.
    """
    name = user_name()
    role = user_role()
    org = user_organization()
    if not name and not role:
        return ""
    bits = ["IMPORTANT context: The user"]
    if name:
        bits.append(f"is {name},")
    if role:
        bits.append(f"a {role}")
    if org:
        bits.append(f"at {org}")
    bits.append(".")
    return " ".join(bits)


def _build_system_prompt() -> str:
    return f"""You are an activity classifier. Given a list of computer activity spans (app name, window title, URL), classify each into exactly one category and write a short description of what the user was doing.

Categories:
- Development: Coding, debugging, terminals, IDEs, reviewing PRs, build tools
- Communication: Async messaging — work email, Slack, Teams chat, iMessage to colleagues
- Meetings: Zoom, Teams, Meet, Webex calls — synchronous audio/video
- Documentation: Writing docs, Notion, wiki editing, work notes
- Planning: Task managers, project planning, roadmaps, calendar management
- Research: Web browsing for work research, reading articles, Stack Overflow, work docs
- Creative: Design work, Figma, Canva, slide creation
- Personal: Non-work browsing only — personal shopping, social media, entertainment, personal banking
- Admin: System settings, IT tasks, software updates, file management
- Break: Idle/AFK periods, music-only, away from desk

{_user_context_line()}

Rules:
- Output ONLY valid JSON — no markdown, no explanation, no thinking tags
- Each entry must have: index (int), category (string), description (string)
- Descriptions should be specific: "Reviewing customer case in Outlook" not "Email"
- If the app is Zoom/Teams/Meet/Webex with a meeting title, always use Meetings
- If the app is a browser with a work URL, classify by what the URL suggests
- AI assistant usage (Claude, ChatGPT, etc.) should be classified by what's being worked on, not as a separate category
- Outlook in any variant = Communication (always work email)
- Teams chat = Communication; Teams call/meeting = Meetings
- When in doubt between Communication and Personal, check the domain — work domains are Communication"""


SYSTEM_PROMPT = _build_system_prompt()


def call_ollama(prompt: str) -> str:
    """Call Ollama API and return the response text."""
    import urllib.request

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,  # Disable Qwen3 thinking mode for direct JSON output
        "options": {
            "temperature": 0.1,
            "num_predict": 4096,
        },
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            return result.get("message", {}).get("content", "")
    except Exception as e:
        logger.error("Ollama call failed: %s", e)
        return ""


def format_spans_for_prompt(spans: list) -> str:
    """Format spans into a prompt for the LLM."""
    lines = []
    for i, span in enumerate(spans, 1):
        apps = ", ".join(sorted(getattr(span, '_all_apps', {span.app_name})))
        parts = [
            f"Index: {i}",
            f"App: {apps}",
        ]
        if span.window_title:
            parts.append(f"Window: {span.window_title}")
        if span.browser_url:
            parts.append(f"URL: {span.browser_url}")
        # Include top window titles for multi-app spans for richer context
        if hasattr(span, '_all_apps') and len(span._all_apps) > 1:
            # Fetch top 3 distinct window titles from observations
            try:
                conn = get_connection()
                placeholders = ",".join("?" * min(len(span.observation_ids), 500))
                sample_ids = span.observation_ids[:500]
                title_rows = conn.execute(
                    f"SELECT DISTINCT window_title FROM observations WHERE id IN ({placeholders}) "
                    f"AND window_title IS NOT NULL AND window_title != '' LIMIT 5",
                    sample_ids,
                ).fetchall()
                conn.close()
                extra_titles = [r["window_title"] for r in title_rows if r["window_title"] != span.window_title]
                if extra_titles:
                    parts.append(f"Also: {'; '.join(t[:60] for t in extra_titles[:3])}")
            except Exception:
                pass
        duration_min = round(span.duration_seconds / 60, 1)
        parts.append(f"Duration: {duration_min}m")
        parts.append(f"Time: {span.start_time.strftime('%H:%M')} - {span.end_time.strftime('%H:%M')}")
        lines.append(" | ".join(parts))

    spans_text = "\n".join(lines)

    return f"""Classify these {len(spans)} activity spans. Return a JSON array with one object per span.

Each object must have: "index" (int), "category" (string from the list), "description" (string, specific).

Spans:
{spans_text}

Return ONLY the JSON array, nothing else."""


def parse_response(response: str, num_spans: int) -> list[dict] | None:
    """Parse LLM response into classifications."""
    # Strip any markdown fencing or thinking tags
    text = response.strip()

    # Remove <think>...</think> blocks (Qwen3 thinking mode)
    import re
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # Remove markdown code fences
    if text.startswith("```"):
        text = re.sub(r'^```\w*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
        text = text.strip()

    # Find JSON array
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        logger.error("No JSON array found in response")
        return None

    try:
        items = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        logger.error("JSON parse error: %s", e)
        logger.debug("Raw response: %s", text[:500])
        return None

    # Validate
    valid = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cat = item.get("category", "")
        # Fuzzy match category
        matched_cat = None
        for c in CATEGORIES:
            if c.lower() == cat.lower() or c.lower() in cat.lower():
                matched_cat = c
                break
        if not matched_cat:
            matched_cat = "Research"  # safe default

        valid.append({
            "index": item.get("index", 0),
            "category": matched_cat,
            "description": item.get("description", ""),
        })

    if len(valid) != num_spans:
        logger.warning("Expected %d classifications, got %d", num_spans, len(valid))
        # Still usable if we got at least some
        if not valid:
            return None

    return valid


def classify_batch(spans: list, db_path: str = DEFAULT_DB_PATH, created_span_ids: list = None) -> int:
    """Classify a batch of spans using Ollama. Returns count of classified spans.

    If `created_span_ids` is provided, any newly-created span IDs are appended to it
    for later memory enrichment.
    """
    prompt = format_spans_for_prompt(spans)
    response = call_ollama(prompt)
    if not response:
        return 0

    classifications = parse_response(response, len(spans))
    if not classifications:
        return 0

    created = 0
    for cls in classifications:
        idx = cls["index"] - 1  # 0-based
        if idx < 0 or idx >= len(spans):
            continue

        span = spans[idx]
        obs_ids = span.observation_ids

        # Fetch observations to build the classified span
        conn = get_connection(db_path)
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

        classified_span = ActivitySpan(
            start_time=first_ts,
            end_time=last_ts,
            duration_seconds=duration,
            app_name=rows[0]["app_name"],
            window_title=rows[0]["window_title"],
            browser_url=rows[0]["browser_url"],
            category=cls["category"],
            description=cls["description"],
            observation_count=len(rows),
            observation_ids=obs_ids,
        )

        span_id = insert_activity_span(classified_span, db_path)
        if created_span_ids is not None:
            created_span_ids.append(span_id)

        # Extract people
        try:
            extract_and_link_people(
                span_id=span_id,
                app_name=classified_span.app_name,
                window_title=classified_span.window_title,
                description=classified_span.description,
                timestamp=first_ts.isoformat(),
                db_path=db_path,
            )
        except Exception:
            pass

        created += 1

    return created


def _user_self_clause() -> str:
    """Tell the model who NOT to extract as a person — the user themselves."""
    name = user_name()
    if not name:
        return "  - The user themselves — never extract the user's own name as a person"
    return f'  - The user themselves — never extract "{name}" or any reordering of it (that\'s the user)'


def _build_memory_system_prompt() -> str:
    role_phrase = ""
    role = user_role()
    if role:
        role_phrase = f" (a {role})"
    return f"""You are a STRICT memory curator for the user{role_phrase}. Given classified work activity spans, extract ONLY concrete, specific items worth remembering in a daily notebook.

QUALITY BAR: It is MUCH better to emit zero items for a span than to emit a weak one. Most spans should yield 0 items.

Return a JSON array. Each item must have:
- "index" (int): span number from the list below
- "kind" (string): one of "ticket" | "person" | "doc" | "link" | "snippet" | "pinned"
- "value" (string): the short main text — MUST be a specific identifier
- "label" (string, optional): short sub-label (e.g., "Jira", "Spreadsheet", "Customer")
- "context" (string): one sentence explaining what was happening with it
- "score" (int 0-10): importance

STRICT RULES PER KIND:

**ticket** — ONLY actual ticket/case IDs.
  Common formats include: "PROJ-1234" (Jira), "INC4012345" / "SR0012345" / "CHG", "CS" + digits, vendor case numbers like "88936/2026". If you don't see an actual ID, SKIP. Never emit a generic word like "case" or "ticket" as a value.

**person** — ONLY actual human names (First Last format). Extract from Teams chat titles like "Chat | Lastname, Firstname" or email subjects.
  DO NOT extract:
  - Single words alone (need both names)
{_user_self_clause()}
  - Product names, acronyms, internal system names — these are NEVER people
  - Company or customer names — those go as context, not person

**doc** — ONLY actual file names with extensions OR specific note titles:
  - "Q3 Use cases.xlsx" ✓
  - "Dashboard - Stories.pdf" ✓
  - "Meeting notes 15 April.md" ✓
  NEVER emit vague things like "personal notes", "presentation slides" with no specific title. SKIP.

**link** — ONLY emit a link item when there's a concrete URL in the span's "URLs:" line that's worth bookmarking.
  MUST:
  - Have the actual URL in the span's data (you MUST include the real URL in the "url" field of your output — no URL means SKIP this span for link)
  - Be specific and high-value (a specific customer page, a specific SharePoint doc, a specific article)
  NEVER:
  - Emit a link item without a full http/https URL — no exceptions
  - Emit for generic inboxes, home pages, new tabs, root pages, launch pages
  - Invent labels for URLs that aren't actually in the span

**snippet** — ONLY a memorable quote, insight, decision, or committed-to action:
  - A complete thought from what they wrote or read
  - Must be at least 6 words and a proper phrase
  NEVER "reviewing X" or "chatting about Y" — those are activity descriptions, not insights. SKIP.

**pinned** — Use sparingly (score 9-10):
  - Explicit escalations
  - Decisions made today
  - Deadlines mentioned
  - Anything someone would genuinely want to remember tomorrow

OUTPUT RULES:
- ONLY valid JSON — no markdown, no thinking, no explanation
- If nothing meets the bar, return []
- NEVER invent details not present in the span
- Prefer 1-2 strong items per span over 5 weak ones
- If the span is just "personal email triage" with no specific person/ticket/doc, emit NOTHING
- Company/customer names belong in context, not as values"""


MEMORY_SYSTEM_PROMPT = _build_memory_system_prompt()


def _format_spans_for_memory(spans: list, db_path: str = DEFAULT_DB_PATH) -> str:
    """Format spans with a sample of raw observation titles for richer memory extraction."""
    conn = get_connection(db_path)
    lines = []
    for i, s in enumerate(spans, 1):
        parts = [
            f"Index: {i}",
            f"Time: {s['start_time'][11:16]}–{s['end_time'][11:16]}",
            f"Category: {s['category'] or 'Unclassified'}",
            f"App: {s['app_name']}",
        ]
        if s['description']:
            parts.append(f"Summary: {s['description'][:240]}")

        # Pull distinct observation window titles + URLs for richer context
        obs = conn.execute(
            """SELECT DISTINCT window_title, browser_url FROM observations
               WHERE datetime(timestamp) BETWEEN datetime(?) AND datetime(?)
               LIMIT 20""",
            (s["start_time"], s["end_time"]),
        ).fetchall()

        titles = []
        urls = []
        for r in obs:
            if r["window_title"] and r["window_title"] not in titles:
                titles.append(r["window_title"])
            if r["browser_url"] and r["browser_url"] not in urls:
                urls.append(r["browser_url"])

        if titles:
            sample_titles = [t[:140] for t in titles[:6]]
            parts.append("Window titles seen: | " + " | ".join(sample_titles))
        if urls:
            sample_urls = [u[:160] for u in urls[:4]]
            parts.append("URLs: | " + " | ".join(sample_urls))

        lines.append("\n".join("  " + p if j > 0 else p for j, p in enumerate(parts)))
    conn.close()
    return "\n\n".join(lines)


def _call_ollama_memory(prompt: str) -> str:
    """Call Ollama with the memory system prompt. Same chat API, different system."""
    import urllib.request
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": MEMORY_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 2048,
        },
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            return result.get("message", {}).get("content", "")
    except Exception as e:
        logger.error("Ollama memory call failed: %s", e)
        return ""


VALID_MEMORY_KINDS = {"ticket", "person", "doc", "link", "snippet", "pinned"}


def enrich_memory_for_spans(span_ids: list[int], db_path: str = DEFAULT_DB_PATH) -> int:
    """Run a Qwen3 memory-extraction pass over the newly-classified spans.

    Returns the count of memory_items inserted.
    """
    if not span_ids:
        return 0

    # Skip ones already enriched
    fresh_ids = [sid for sid in span_ids if not has_memory_items_for_span(sid, db_path)]
    if not fresh_ids:
        return 0

    conn = get_connection(db_path)
    placeholders = ",".join("?" * len(fresh_ids))
    rows = conn.execute(
        f"""SELECT id, start_time, end_time, app_name, window_title, browser_url,
                   category, description
            FROM activity_spans
            WHERE id IN ({placeholders})
            ORDER BY start_time""",
        fresh_ids,
    ).fetchall()
    conn.close()

    if not rows:
        return 0

    # Skip spans with no useful text — memory extraction on empty metadata is wasteful
    useful_rows = [r for r in rows if (r["window_title"] or r["description"] or r["browser_url"])]
    if not useful_rows:
        return 0

    prompt_body = _format_spans_for_memory(useful_rows, db_path)
    user_prompt = (
        f"Extract memory-worthy items from these {len(useful_rows)} spans. "
        f"Return ONLY a JSON array. If nothing is worth remembering in a span, just skip it.\n\n"
        f"Spans:\n{prompt_body}\n\nReturn only the JSON array."
    )
    response = _call_ollama_memory(user_prompt)
    if not response:
        return 0

    # Parse — reuse strip logic
    import re as _re
    text = response.strip()
    text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()
    if text.startswith("```"):
        text = _re.sub(r"^```\w*\n?", "", text)
        text = _re.sub(r"\n?```$", "", text)
        text = text.strip()
    start_b = text.find("[")
    end_b = text.rfind("]")
    if start_b == -1 or end_b == -1:
        logger.warning("Memory enrichment: no JSON array in response")
        return 0
    try:
        items = json.loads(text[start_b:end_b + 1])
    except json.JSONDecodeError as e:
        logger.warning("Memory enrichment: JSON parse failed — %s", e)
        return 0

    inserted = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int) or idx < 1 or idx > len(useful_rows):
            continue
        kind = (item.get("kind") or "").strip().lower()
        if kind not in VALID_MEMORY_KINDS:
            continue
        value = (item.get("value") or "").strip()
        if not value or len(value) > 300:
            continue
        row = useful_rows[idx - 1]
        span_date = row["start_time"][:10]
        label = (item.get("label") or "").strip() or None
        context = (item.get("context") or "").strip() or None
        score_raw = item.get("score", 5)
        try:
            score = max(0, min(10, int(score_raw)))
        except (ValueError, TypeError):
            score = 5
        # Prefer the span's URL if the LLM didn't specify one
        url = (item.get("url") or "").strip() or (row["browser_url"] if kind in ("link", "doc", "ticket") else None)

        try:
            insert_memory_item(
                span_id=row["id"],
                kind=kind,
                value=value[:300],
                label=label[:60] if label else None,
                context=context[:400] if context else None,
                url=url,
                score=score,
                span_date=span_date,
                db_path=db_path,
            )
            inserted += 1
        except Exception as e:
            logger.debug("insert_memory_item failed: %s", e)

    return inserted


def check_ollama_running() -> bool:
    """Check if Ollama is running and the model is available."""
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            models = [m["name"] for m in data.get("models", [])]
            return any(OLLAMA_MODEL.split(":")[0] in m for m in models)
    except Exception:
        return False


def _rows_to_sub_span(rows, parent_block):
    """Create a sub-span from a slice of observation rows."""
    from copy import copy
    sub = copy(parent_block)
    sub.observation_ids = [r["id"] for r in rows]
    sub.observation_count = len(rows)
    sub.start_time = datetime.fromisoformat(rows[0]["timestamp"])
    sub.end_time = datetime.fromisoformat(rows[-1]["timestamp"])
    sub.duration_seconds = max(5, int((sub.end_time - sub.start_time).total_seconds()))
    # Pick most common window title and URL from this chunk
    titles = [r["window_title"] for r in rows if r["window_title"]]
    if titles:
        sub.window_title = max(set(titles), key=titles.count)
    urls = [r["browser_url"] for r in rows if r["browser_url"]]
    if urls:
        sub.browser_url = max(set(urls), key=urls.count)
    # Track apps in this chunk
    apps = set(r["app_name"] for r in rows if r["app_name"])
    if len(apps) > 1:
        sub._all_apps = apps
    elif apps:
        sub.app_name = apps.pop()
    return sub


def merge_micro_spans(spans: list, gap_seconds: int = 30, max_block_minutes: int = 20, db_path: str = DEFAULT_DB_PATH) -> list:
    """Pre-merge tiny spans into larger blocks before classification.

    Consecutive spans within gap_seconds of each other get merged into one,
    combining their observation_ids. Blocks are also split if they exceed
    max_block_minutes, so the timeline stays granular.
    """
    if not spans:
        return []

    # Step 1: merge micro-gaps (<gap_seconds)
    merged = [spans[0]]
    for span in spans[1:]:
        prev = merged[-1]
        gap = (span.start_time - prev.end_time).total_seconds()
        if gap <= gap_seconds:
            # Merge: extend the previous span
            prev.end_time = max(prev.end_time, span.end_time)
            prev.duration_seconds = max(5, int((prev.end_time - prev.start_time).total_seconds()))
            prev.observation_ids.extend(span.observation_ids)
            prev.observation_count += span.observation_count
            # Keep the most informative window title (longer = more info)
            if span.window_title and (not prev.window_title or len(span.window_title) > len(prev.window_title)):
                prev.window_title = span.window_title
            # Keep the most informative URL
            if span.browser_url and not prev.browser_url:
                prev.browser_url = span.browser_url
            # Track all apps seen
            if span.app_name != prev.app_name:
                if not hasattr(prev, '_all_apps'):
                    prev._all_apps = {prev.app_name}
                prev._all_apps.add(span.app_name)
        else:
            merged.append(span)

    # Step 2: split blocks that exceed max_block_minutes using actual observation timestamps
    max_secs = max_block_minutes * 60
    final = []
    for block in merged:
        if block.duration_seconds <= max_secs:
            final.append(block)
        else:
            # Fetch actual observation timestamps to split intelligently
            obs_ids = block.observation_ids
            conn = get_connection(db_path)
            placeholders = ",".join("?" * len(obs_ids))
            rows = conn.execute(
                f"SELECT id, timestamp, app_name, window_title, browser_url FROM observations "
                f"WHERE id IN ({placeholders}) ORDER BY timestamp",
                obs_ids,
            ).fetchall()
            conn.close()

            if not rows:
                final.append(block)
                continue

            # Split into time-based chunks
            from datetime import timedelta
            chunk_start = 0
            first_ts = datetime.fromisoformat(rows[0]["timestamp"])
            for k, row in enumerate(rows):
                ts = datetime.fromisoformat(row["timestamp"])
                elapsed = (ts - datetime.fromisoformat(rows[chunk_start]["timestamp"])).total_seconds()
                if elapsed >= max_secs and k > chunk_start:
                    # Emit this chunk
                    chunk_rows = rows[chunk_start:k]
                    sub = _rows_to_sub_span(chunk_rows, block)
                    final.append(sub)
                    chunk_start = k
            # Emit remaining
            if chunk_start < len(rows):
                chunk_rows = rows[chunk_start:]
                sub = _rows_to_sub_span(chunk_rows, block)
                final.append(sub)

    logger.info("Pre-merged %d micro-spans into %d blocks.", len(spans), len(final))
    return final


def run_once(db_path: str = DEFAULT_DB_PATH, enrich: bool = True) -> int:
    """Run one classification pass. Returns total spans classified.

    If `enrich` is True, a second Qwen3 pass extracts memory-worthy items
    from newly-classified spans (tickets, people, docs, snippets, etc.)
    and writes them to memory_items.
    """
    spans = get_unclassified_spans(db_path)
    if not spans:
        logger.info("No unclassified spans.")
        return 0

    logger.info("Found %d unclassified spans to classify.", len(spans))

    # Pre-merge micro-spans into meaningful blocks
    spans = merge_micro_spans(spans, db_path=db_path)

    total = 0
    new_span_ids: list[int] = []

    # Process in batches
    for i in range(0, len(spans), BATCH_SIZE):
        batch = spans[i:i + BATCH_SIZE]
        logger.info("Classifying batch %d-%d of %d...", i + 1, min(i + BATCH_SIZE, len(spans)), len(spans))
        classified = classify_batch(batch, db_path, created_span_ids=new_span_ids)
        total += classified
        logger.info("Classified %d/%d in this batch.", classified, len(batch))

    logger.info("Total classified: %d/%d spans.", total, len(spans))

    # Memory enrichment pass — reuse the already-loaded Qwen3 model
    if enrich and new_span_ids:
        try:
            # Chunk the enrichment so each LLM call stays under token budget
            ENRICH_BATCH = 12
            total_mem = 0
            for i in range(0, len(new_span_ids), ENRICH_BATCH):
                chunk = new_span_ids[i:i + ENRICH_BATCH]
                logger.info("Memory enrichment batch %d-%d of %d...",
                            i + 1, min(i + ENRICH_BATCH, len(new_span_ids)), len(new_span_ids))
                n = enrich_memory_for_spans(chunk, db_path)
                total_mem += n
                logger.info("  → extracted %d memory items", n)
            logger.info("Memory enrichment complete: %d items across %d spans.",
                        total_mem, len(new_span_ids))
        except Exception:
            logger.exception("Memory enrichment failed")

    return total


def run_daemon(db_path: str = DEFAULT_DB_PATH, interval: int = INTERVAL_SECONDS):
    """Run as a daemon, classifying every `interval` seconds."""
    init_db(db_path)
    logger.info("Local classifier daemon started (model=%s, interval=%ds)", OLLAMA_MODEL, interval)

    while True:
        try:
            if not check_ollama_running():
                logger.warning("Ollama not running or model not available. Skipping.")
            else:
                # Only classify during work hours (8am-9pm)
                hour = datetime.now().hour
                if 8 <= hour < 21:
                    run_once(db_path)
                else:
                    logger.debug("Outside work hours (%d:00). Skipping.", hour)
        except Exception:
            logger.exception("Error during classification pass")

        time.sleep(interval)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Local activity classifier using Ollama")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=INTERVAL_SECONDS, help="Seconds between runs (daemon mode)")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite database")
    parser.add_argument("--no-enrich", action="store_true", help="Skip the memory enrichment pass")
    parser.add_argument("--backfill-memory", action="store_true",
                        help="Run memory enrichment on recent already-classified spans (last 14 days) and exit")
    args = parser.parse_args()

    if args.backfill_memory:
        init_db(args.db)
        from datetime import date, timedelta
        conn = get_connection(args.db)
        start = (date.today() - timedelta(days=14)).isoformat()
        rows = conn.execute(
            "SELECT id FROM activity_spans WHERE date(start_time) >= ? ORDER BY start_time",
            (start,),
        ).fetchall()
        conn.close()
        ids = [r["id"] for r in rows]
        logger.info("Backfilling memory for %d spans since %s...", len(ids), start)
        ENRICH_BATCH = 12
        total = 0
        for i in range(0, len(ids), ENRICH_BATCH):
            chunk = ids[i:i + ENRICH_BATCH]
            logger.info("  Backfill batch %d-%d of %d...", i + 1, min(i + ENRICH_BATCH, len(ids)), len(ids))
            total += enrich_memory_for_spans(chunk, args.db)
        logger.info("Backfill complete: %d memory items inserted.", total)
    elif args.once:
        init_db(args.db)
        run_once(args.db, enrich=not args.no_enrich)
    else:
        run_daemon(args.db, args.interval)
