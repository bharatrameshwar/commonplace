# Contributing

Thanks for considering a contribution.

## Ground rules

- **PR-only.** Main is locked. All changes go through pull requests.
- **One concern per PR.** Easier to review, easier to revert.
- **Keep changes small.** If a PR has more than ~300 lines of diff, consider splitting it.
- **No personal data in prompts or code.** User identity belongs in `config.yaml` only.

## Dev setup

```bash
git clone https://github.com/<your-fork>/commonplace.git
cd commonplace
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp config.example.yaml config.yaml   # edit as needed
```

Run components without launchd while developing:

```bash
.venv/bin/python daemon.py            # capture loop (foreground)
.venv/bin/python dashboard.py         # http://127.0.0.1:8420
.venv/bin/python local_classifier.py --once   # classify pending spans
```

## Code style

- `ruff` for lint. Run `.venv/bin/ruff check .` before pushing.
- Type hints where they pay rent — module-level public functions, anything touching the DB.
- Comments explain *why*, not *what*.

## Tests

Limited coverage today. New modules should land with at least a happy-path test in `tests/`. Run with:

```bash
.venv/bin/pytest
```

## Reporting bugs

Open an issue with:
- macOS version
- Python version
- What you expected, what happened
- Relevant log excerpt from `~/.local/share/commonplace/*.log`

## Cross-platform ports

A Windows or Linux port would be very welcome. The capture layer (`tracker/`) and launchd plists are platform-specific — everything else is portable. Open an issue first to scope it.
