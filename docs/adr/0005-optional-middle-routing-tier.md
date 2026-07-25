# ADR-0005: Optional middle routing tier

Status: accepted (2026-07-25)

## Context

Routing was binary: a prompt either stayed on the cheap session model or went to the heavy-task
agent on the most expensive model. Real work is not binary. A contained bug fix, a focused
multi-file edit, or writing a test suite is more than the cheap tier handles well and much less
than the top tier needs to be paid for.

With only two tiers, every prompt above the threshold pays top-tier rates. Lowering the threshold
to catch mid-weight work makes that worse, because it sends more prompts to the most expensive
model; raising it leaves mid-weight work on a model that will struggle. The threshold is a single
dial being asked to express two different decisions.

ADR-0003 already established that per-project overrides may tune routing behaviour but may never
name a model, because the agent files are generated from the global config at install time.

## Decision

- Add an optional `models.standard` and a `complexity.standard_threshold`. Scores are banded:
  `>= threshold` routes to the complex tier, `>= standard_threshold` (and below `threshold`) routes
  to the middle tier, and anything lower is answered in-session.
- Add `routing.tiers`: `"auto"` (default), `2`, or `3`. Auto means three tiers when a valid
  `models.standard` exists and two otherwise, so simply adding a middle model is enough to enable
  it and an explicit `2` disables it without deleting configuration.
- Generate a second agent file, `mid-task-<model>.md`, from its own template. The top tier keeps
  the `heavy-task-` prefix and the `agent_file` manifest key unchanged, so existing installs,
  manifests, and CLAUDE.md policy blocks remain valid.
- `standard_threshold` is forced strictly below `threshold`. An overlapping pair would make the
  middle band unreachable while the config claimed otherwise; clamping with a warning is the same
  fail-open treatment `threshold` already gets.
- The middle tier is never named in a project override — only `routing.tiers` and the two
  thresholds are overridable, preserving ADR-0003's rule that a lower-trust file cannot introduce a
  model name into Claude's context.
- Re-installing with `models.standard` removed deletes the middle agent, so downgrading from three
  tiers to two does not leave an orphaned agent behind.

## Consequences

- Two tiers remains the default. An existing config gains nothing and loses nothing until it opts
  in by adding `models.standard`; every pre-existing test passed unchanged through this change.
- The CLAUDE.md policy block is now tier-agnostic: it tells Claude to spawn the agent *named in the
  directive* rather than naming `heavy-task` itself. Re-installing updates the managed block in
  place. A stale block from an older install still works, because the directive text it keys on is
  still a `[model-switcher] ... MANDATORY ROUTING POLICY` line.
- Cost behaviour is now a three-way trade-off rather than a slider, and it is possible to
  misconfigure it into paying *more* — for example a middle model as expensive as the top one. The
  statusline's savings view (ADR-0006) is what makes that visible.
- Three tiers means two subagent definitions to keep in sync, and a prompt near a band edge can
  route differently after a small scoring change. That volatility is bounded by the bands being
  explicit and inspectable rather than implicit.
- The middle agent is told to escalate rather than half-finish when a task turns out to be larger
  than its band suggested, since the router only sees the prompt and cannot know what the work will
  become.
