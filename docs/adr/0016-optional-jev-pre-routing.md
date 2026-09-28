# ADR-0016: Optional Jev evaluation before routing

## Context

The local router is cheap, deterministic and private, but its vocabulary features can confuse
requests with similar words and different intent. We want to evaluate Jev as a semantic routing
signal, inspect its complete exchange, and offer Sonnet → Opus → Fable as the model ladder.
Existing users and contributors must retain a working setup without an account or network.
See [research](../jev-research.md) for official sources and unresolved evaluation questions.

## Decision

Keep `score_prompt`, `select_tier`, learning and offline reports unchanged. Add `evaluate_route`
as the shared integration boundary for the prompt and generic-agent hooks. It computes the local
baseline then optionally calls `jev_router`. Jev uses a single Choice question over active model
tiers at the fixed official TypeSafe endpoint. Model IDs and choices are validated before use.

Fresh installs configure Sonnet (simple), Opus (standard), Fable (complex), with local thresholds
3 and 7. Existing config files are preserved, including two-tier setups and disabled routing.
Jev remains disabled by default. `shadow` logs recommendations; `route` accepts only structurally
valid, sufficiently confident results. The default confidence gate is 0.7 and model version is
pinned to `jev-1.13.0`. These policy values need workload-specific evaluation.

Run HTTP in a stdlib subprocess with a 3-second default deadline (configurable 0.1–10 seconds).
Do not retry or follow redirects. Bound request input and response bytes. Every failure,
missing credential, invalid answer and low-confidence answer uses the existing local route.
Subagent/command exclusions and deliberate agent selections still take precedence.

Credentials come from `TYPESAFE_API_KEY` or the owner-only `jev-api-key` in the install directory.
Do not accept API URLs or keys from project config. Projects can only turn Jev off.

The user-requested content logs are an explicit exception to the original no-prompt-logging
rule: `jev.log_content: true` writes the actual state, instructions, request and response, with
credential redaction, in private rotating JSONL files. Otherwise only metadata and validated
decisions are recorded. Request IDs connect the request and result. Logs and credentials survive
uninstall as user data; users can remove them explicitly when no longer wanted.

## Consequences

Local-only installs retain zero network calls from hooks and do not log prompt content.
Opted-in installs incur network latency and send the current task to TypeSafe. The runtime still
has no third-party dependencies. `explain` describes only the local baseline; `jev` explicitly
performs one live evaluation, and `jev --offline` previews its request. Jev choices are not a
security boundary, and confident classification can still be wrong. Full logs may contain
sensitive prose despite credential redaction; do not commit or publish them.

The statusline remains a Claude usage estimate and excludes Jev cost. Model selection is still
implemented through delegation; it does not replace the current Claude Code session model.
