# ADR-0003: Routing toggle and per-project override file

Status: accepted (2026-07-10)

## Context

Delegation is all-or-nothing once installed: every prompt in every local session is scored, and complex prompts are directed to the heavy-task agent. Two situations make that too coarse:

- Sessions deliberately run on the heavy model (e.g. the session model set to Fable 5). Delegating there spawns a subagent on the same model — pure overhead with no cost saving.
- Different repos have different economics: a scratch repo may not justify heavy-model routing at all, while one critical repo may want a different threshold than the global default.

Uninstalling to pause routing is heavy-handed (it also removes the statusline), and the global config has no per-repo dimension. The `UserPromptSubmit` hook payload includes the session's `cwd`, so the hook can know which project a prompt belongs to without any new mechanism.

## Decision

- Add a `routing.enabled` flag to `~/.claude/model-switcher/config.json`. Absent or `true` means routing is on (backwards compatible with every existing install). When effective `enabled` is `false`, the hook emits nothing at all: no scoring, no delegation directive, and no setup nags — the entire delegation surface pauses. The statusline is a separate script and keeps tracking cost.
- Add a per-project override file, `<project>/.claude/model-switcher.json`, read from the hook payload's `cwd`. Only its `routing` and `complexity` sections are honoured, shallow-merged over the global config. `models` and `pricing_usd_per_mtok` are never overridable per project: the heavy-task agent file is generated from the global config at install time, and a project-supplied model name would be interpolated into Claude's context from a lower-trust source.
- The override file is untrusted input, handled like the prompt itself: stdlib JSON parsing only, a 64 KB size cap, contents never logged, and every failure (missing, malformed, wrong types, oversized) falls open to the global config. Non-bool `enabled` values are ignored with a one-line stderr warning.
- Toggle and override changes apply on the next prompt — the hook re-reads config per prompt, so no re-install is needed.

## Consequences

- Routing can be paused globally (or enabled only where it pays off) without touching the install, and a repo can opt in/out or re-tune the threshold independently.
- A new untrusted input surface (the project file) exists, mitigated by the fail-open parsing rules above and covered by hostile-input tests.
- When routing is disabled, the models/pricing setup nags are also suppressed; a user who disables routing before configuring models will not be prompted to configure them until routing is re-enabled. The statusline's own pricing warning still appears.
- The project override is invisible to the installer and the agent generator: a project cannot change which heavy-task agent exists, only whether and when the directive to use it fires.
