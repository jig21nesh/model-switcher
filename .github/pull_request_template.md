## What and why

<!-- What changed, and what problem it solves. Link the issue if there is one. -->

## Test evidence

<!-- Paste the relevant output. -->

- [ ] `pytest tests/ -q` passes
- [ ] Per-file coverage holds at 80% line and branch (`tools/check_coverage.py`)
- [ ] Hostile-input cases added for any new input surface

## Checklist

- [ ] Router still fails open (exit 0, no output) on every error path it can reach
- [ ] Statusline still prints exactly one line on every error path
- [ ] No new runtime dependencies; no clock, network or randomness in the scoring path
- [ ] No prompt content or pricing values written to logs
- [ ] `README.md` updated if user-visible behaviour changed
- [ ] ADR added in `docs/adr/` if this introduces a new pattern or trade-off

## Follow-ups deferred

<!-- Anything intentionally left out of this PR. -->
