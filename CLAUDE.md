# model-switcher — project rules

Per-prompt model routing + offline cost statusline for Claude Code. See README.md and docs/adr/.

## Stack and layout

- Python 3.12+ stdlib only at runtime; `pytest`/`pytest-cov` are the only dev dependencies.
- `hooks/complexity_router.py` — UserPromptSubmit hook (scoring + delegation directive).
- `statusline/cost_statusline.py` — statusline command (offline cost from transcript).
- `scripts/merge_settings.py` — settings.json install/uninstall logic (all merge logic lives here, not in bash).
- `install.sh` — thin copier/orchestrator; keep logic out of it.
- `agents/heavy-task.md` — subagent template; `model:` line is stamped by the installer.

## Hard rules

- Hook and statusline scripts must never crash or block: the router fails open (exit 0, no output), the statusline always prints a line.
- All stdin, prompt text, and transcript content is untrusted input: stdlib JSON parsing only, never eval, never interpolate it into shell commands, never write it to logs.
- No new runtime dependencies — these scripts run on every prompt in every session.
- No `Date`/network calls in the scoring path: scoring must stay deterministic and offline.
- Never log prompt content or pricing config values; errors go to stderr as one line.

## Testing

- `.venv/bin/python -m pytest tests/ --cov=hooks --cov=statusline --cov=scripts --cov-branch`
- 80% line and branch coverage floor per file; include hostile-input cases (malformed stdin, path traversal in session_id, shell metacharacters in prompts) for any new input surface.

## Docs

- User-visible behaviour changes update README.md (and the Mermaid diagram if flow changes) in the same PR.
- New patterns/integrations get an ADR in docs/adr/ (numbered, context/decision/consequences).
