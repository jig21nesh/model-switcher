# Contributing to model-switcher

Thanks for taking the time. This project runs code on **every prompt of every local Claude Code
session**, so the bar for changes to `hooks/` and `statusline/` is deliberately high.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install pytest pytest-cov
```

Runtime code is **stdlib-only**. `pytest` and `pytest-cov` are the only dependencies you install;
`ruff` and `shellcheck` run in CI only (see `.ruff.toml`), so you never need them locally — though
`uvx ruff check .` matches CI exactly if you want the fast feedback.

## The checks CI runs

```sh
.venv/bin/python -m pytest tests/ -q                      # full suite
.venv/bin/python -m pytest tests/ -q -m lifecycle         # real install.sh against a temp CLAUDE_DIR
.venv/bin/python -m pytest tests/ -q --cov --cov-branch \
  --cov-report=json:coverage.json --cov-report=term
.venv/bin/python tools/check_coverage.py coverage.json --floor 80
uvx ruff check .                                          # optional locally, required in CI
```

Tests run on Python 3.10–3.14. The coverage floor is **80% line and 80% branch, enforced per
file** — a global average that hides one thin file does not pass.

## Hard rules

These are not style preferences; breaking one is a bug even if the tests pass.

- **The router fails open.** Any failure in `hooks/complexity_router.py` must exit 0 with no
  output. A routing bug must never block or erase a user's prompt. Exit 2 would erase it.
- **The statusline always prints exactly one line**, whatever goes wrong.
- **No runtime dependencies.** These scripts run on every prompt; stdlib only.
- **Scoring stays deterministic and offline.** No clock reads, no network, no randomness anywhere
  in the scoring path. It must be reproducible from its inputs alone.
- **All input is hostile.** Prompt text, transcript contents, `config.json`, and project override
  files are untrusted: parse with the stdlib JSON parser, never `eval`, never interpolate into a
  shell command, never write to a log.
- **Never log prompt content or pricing values.** Errors go to stderr as a single line.

## Tests we expect on a PR

Cover the happy path *and* the hostile path. For any new input surface, that means at minimum:
malformed and truncated JSON, wrong types, oversized input, path traversal in anything used to
build a filename, and shell metacharacters in text that reaches a subprocess or Claude's context.
No padding tests — each one should exercise real behaviour or a real error path.

## Docs and decisions

- User-visible behaviour changes update `README.md` in the same PR (and the Mermaid diagram if the
  flow changed).
- A new pattern, integration, config surface, or significant trade-off gets a numbered ADR in
  `docs/adr/` — context, decision, consequences — in the same PR as the change.

## Pull requests

`main` is protected. Branch (`feat/…`, `fix/…`, `chore/…`, `refactor/…`), push, open a PR, and let
CI go green. Keep commit subjects imperative and under 72 characters (`add X`, not `Added X`).
Open an issue first if you want to discuss a larger change.
