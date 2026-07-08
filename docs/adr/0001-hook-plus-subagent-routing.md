# ADR-0001: Hook + subagent delegation for model routing; statusline for cost display

Status: accepted (2026-07-08)

## Context

The goal is per-prompt model routing (complex prompts on Opus, simple on Sonnet) and a deterministic, offline token-cost display on every response, applied globally to all local Claude Code sessions. Verified against official Claude Code documentation:

- No hook output field can change the main session's model; the model is fixed per session (`/model`, `--model`, `settings.json`). Editing `settings.json` mid-session has no effect on the live session.
- Subagents in `~/.claude/agents/*.md` support a `model` frontmatter field (aliases or full IDs) and apply to all projects, CLI and VS Code.
- A `UserPromptSubmit` hook receives every prompt and can inject `additionalContext`; it cannot modify the prompt or pick a model.
- A `Stop` hook's stdout is never shown in the normal chat view, so it cannot print a cost line into responses. Having Claude itself print a cost line would be model-dependent, not deterministic.
- The statusline command receives `model`, `transcript_path`, and `cost.total_cost_usd`, refreshes after every assistant message, and works in CLI and VS Code. The session transcript (`.jsonl`) carries per-message `usage` objects including cache token splits (verified empirically on Claude Code 2.1.204).

## Decision

- Run the session on the cheap model (Sonnet, set by the installer). A `UserPromptSubmit` hook scores each prompt with a deterministic offline heuristic; at or above the threshold it injects a directive to delegate to the `heavy-task` subagent, which declares the configured heavy model (Opus by default).
- Compute cost in a statusline command that stream-parses the transcript and prices tokens with a user-maintained table in `~/.claude/model-switcher/config.json`. Fall back to Claude Code's built-in estimate when the transcript has no usage data.
- Unconfigured models: the hook injects an instruction so Claude asks the user to confirm and persists the choice. Unconfigured pricing: statusline shows a warning with the official pricing URL, and the hook asks once per session.
- The router fails open: any hook failure exits 0 with no output, so the prompt proceeds on the session default. Blocking user prompts on a routing bug is worse than a missed delegation.
- If the user already has a custom statusline, the installer records it and the cost statusline wraps it (runs it, appends the cost segment) instead of replacing it.

## Consequences

- The expensive model is only used when a prompt crosses the threshold; the orchestrating loop stays on Sonnet. Delegation adds subagent startup overhead and the relay step on complex prompts.
- Routing is advisory at the model level: Claude follows the injected directive rather than a hard switch, because the platform offers no hard per-prompt switch.
- The cost display is deterministic but is an estimate of billing, not the bill; sidechain usage is included, but server-side charges not present in the transcript are not.
- Cloud/claude.ai sessions are unaffected — local `~/.claude` configuration does not reach them.
- The heavy-task agent file is generated at install time; changing `models.complex` requires re-running `install.sh`.
