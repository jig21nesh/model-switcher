# model-switcher — project rules

Per-prompt model routing + offline cost statusline for Claude Code. See README.md and docs/adr/.

## Stack and layout

- Python 3.10+ stdlib only at runtime (CI matrix covers 3.10–3.14); `pytest`/`pytest-cov` are the
  only dev dependencies. `ruff` and `shellcheck` are CI-only and never installed by contributors.
- `hooks/complexity_router.py` — UserPromptSubmit hook (scoring + delegation directive).
- `statusline/cost_statusline.py` — statusline command (offline cost from transcript).
- `scripts/merge_settings.py` — settings.json install/uninstall logic (all merge logic lives here, not in bash).
- `scripts/manage_claude_md.py` — marker-managed routing-policy block in the user's global CLAUDE.md; block text ships in `config/claude-md-section.md`.
- `scripts/cli.py` + `bin/model-switcher` — the `pricing`/`learn`/`explain` CLI. The shim is copied
  into the install directory, so it must work from both layouts: repo (modules in subdirectories)
  and installed (everything flat). Anything a subcommand imports has to be copied by `install.sh`.
- `install.sh` — thin copier/orchestrator; keep logic out of it.
- `agents/heavy-task.md` — subagent template; `model:` line is stamped by the installer.
- `tools/` — repo tooling that is not part of the shipped product and is not coverage-measured.

## Hard rules

- Hook and statusline scripts must never crash or block: the router fails open (exit 0, no output), the statusline always prints a line.
- All stdin, prompt text, and transcript content is untrusted input: stdlib JSON parsing only, never eval, never interpolate it into shell commands, never write it to logs.
- No new runtime dependencies — these scripts run on every prompt in every session.
- No `Date`/network calls in the scoring path: scoring must stay deterministic and offline.
- Never log prompt content or pricing config values; errors go to stderr as one line.

## Testing

- `.venv/bin/python -m pytest tests/ -q --cov --cov-branch --cov-report=json:coverage.json`
  then `.venv/bin/python tools/check_coverage.py coverage.json --floor 80`.
- 80% line and branch floor is enforced **per file** by `tools/check_coverage.py`; a global average
  is not sufficient. Include hostile-input cases (malformed stdin, path traversal in session_id,
  shell metacharacters in prompts) for any new input surface.
- `-m lifecycle` runs the real `install.sh` against a throwaway `CLAUDE_DIR` and asserts uninstall
  restores `settings.json` and `CLAUDE.md` byte-for-byte. Never point it at a real `~/.claude`.

## Docs

- User-visible behaviour changes update README.md (and the Mermaid diagram if flow changes) in the same PR.
- New patterns/integrations get an ADR in docs/adr/ (numbered, context/decision/consequences).
