# Commonplace

A local-first macOS activity tracker with AI-powered classification, a daily commonplace book, and an MCP server for Claude.

Everything stays on your machine. No cloud, no telemetry, no accounts.

## What it does

- **Captures** the frontmost app, window title, and browser URL every 5 seconds. Takes a screenshot every 30 seconds.
- **Classifies** activity into categories (Development, Meetings, Communication, …) using a local Ollama model.
- **Remembers** the people you talked to, the documents you opened, the tickets you touched, the links you read — surfaced as a daily *commonplace book*.
- **Talks to Claude** via an MCP server, so you can ask "what did I read about X last week?" or "who did I last meet with from Acme?" and get answers grounded in your actual activity.

## Screenshots

*(coming with the first release post)*

## Requirements

- macOS (the capture daemon uses AppleScript and `screencapture`)
- Python 3.10+
- [Ollama](https://ollama.com/) running locally with a model pulled (default: `qwen3:8b`)

## Install

```bash
git clone https://github.com/bharatrameshwar/commonplace.git
cd commonplace

# Create venv and install
python3 -m venv .venv
.venv/bin/pip install -e .

# Configure
cp config.example.yaml config.yaml
$EDITOR config.yaml   # set user_profile.name, role, self_aliases

# Pull the AI model (or edit config.yaml to use a different one)
ollama pull qwen3:8b

# Install launchd agents (capture, dashboard, classifier, cleanup)
./scripts/install.sh

# Open the dashboard
open http://127.0.0.1:8420
```

To stop everything:

```bash
./scripts/install.sh --uninstall
```

## Configuration

`config.yaml` is read at startup. The most important section is `user_profile`:

```yaml
user_profile:
  name: "Jane Doe"
  self_aliases:
    - "jane doe"
    - "doe, jane"
  role: "Software engineer"
  organization: "Acme Corp"     # optional, used in classifier prompt
  person_stopwords:             # words that look like names but aren't
    - "Acme"
    - "Project Phoenix"
```

Filling these in keeps:
- Your own name out of the people-detector and the daily commonplace book.
- AI classifications grounded in your actual role.
- Internal product/project names from being mistaken for people.

## What runs where

| Component | Path | Purpose |
|---|---|---|
| Capture daemon | `daemon.py` | Polls active app every 5s; takes screenshots every 30s |
| Classifier | `local_classifier.py` | Runs every 5 minutes; categorises spans + extracts memory items via Ollama |
| Dashboard | `dashboard.py` | Flask server on `127.0.0.1:8420` |
| Cleanup | `cleanup.py` | Daily at 3am; prunes old screenshots/observations; refreshes per-person weekly summaries |
| MCP server | `mcp_server.py` | Tools for Claude/Claude Code to search your activity |

Data lives in `~/.local/share/commonplace/`.

## Pages

- `/` — today's dashboard (timeline, hourly bars, focus score)
- `/reports` — weekly trends and category comparison
- `/search` — the **Commonplace Book**: today's people, documents, links, tickets, snippets — searchable, with a click-to-zoom panel for each person showing their weekly summary

## MCP server

Add to your Claude Desktop or Claude Code config:

```json
{
  "mcpServers": {
    "commonplace": {
      "command": "/path/to/commonplace/.venv/bin/python",
      "args": ["/path/to/commonplace/mcp_server.py"]
    }
  }
}
```

Then ask Claude things like *"what was I working on yesterday afternoon?"* or *"summarise my last week of meetings with Lochner."*

## Architecture notes

SQLite for storage (FTS5 for search). Plain Flask for the dashboard. PyYAML for config. No JS framework — the templates are server-rendered HTML with vanilla JS sprinkles.

Capture is macOS-specific (`AppleScript` for active window + browser URL, `screencapture` for screenshots, `launchd` for scheduling). A Windows port would need to swap those three layers — everything else (DB, dashboard, classifier, MCP, people extraction) is platform-agnostic Python.

## Contributing

PR-only workflow. Main is locked. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
