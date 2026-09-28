# Jev integration verification

Date: 2026-09-29. Local environment: macOS, Python 3.14.5.

| Check | Result |
|---|---|
| Full pytest suite with branch coverage | 1,077 passed |
| Per-file coverage gate | All 16 measured files meet 80% lines and branches |
| Jev evaluator coverage | 99.2% statements, 97.2% branches |
| Routing log viewer coverage | 100% statements, 94.0% branches |
| Ruff | Passed |
| Shell syntax (`bash -n install.sh`) | Passed |
| Whitespace (`git diff --check`) | Passed |
| Installed CLI offline request preview | Contains Sonnet, Opus, Fable and the exact Choice question |
| Live TypeSafe API evaluation | Three synthetic prompts returned HTTP 200; all selected the intended tier |

## Live smoke results

The saved private credential was used against the official TypeSafe endpoint. Only synthetic
prompts were submitted; automatic routing and persistent content logging were disabled during
these initial smoke checks (subsequently enabled with explicit user approval, below).
Content logging was enabled in memory for these three calls, producing paired request/result
records in the local `logs/jev.jsonl`. The credential was verified absent from those logs.

| Synthetic task | Jev choice | Confidence | End-to-end evaluation latency |
|---|---|---|---|
| Explain Python's `len()` | simple / Sonnet | 1.00 | 590.7 ms |
| Validate one endpoint and add focused unit tests | standard / Opus | 0.99 | 442.2 ms |
| Design a multi-region payments migration to event sourcing | complex / Fable | 1.00 | 406.6 ms |

These latencies include the worker process and network round trip. Three hand-picked examples
are a connectivity and behavior smoke test, not an accuracy benchmark or latency distribution.

## Independent offline/Jev comparison checks

After adding schema version 2 and the `logs` viewer, the same three synthetic requests were
evaluated with the installed learned classifier. The local config was unchanged; only the test
process enabled evaluations and content logging. The standalone installed CLI successfully
displayed all three comparisons, and the saved credential was absent from the logs.

| Task | Offline score / model | Jev model / confidence | Agreement | Latency |
|---|---|---|---|---|
| Python `len()` lookup | 0 / Sonnet | Sonnet / 1.00 | yes | 600.2 ms |
| Endpoint validation and tests | 4 / Opus | Opus / 0.99 | yes | 572.9 ms |
| Payments migration design | 8 / Fable | Fable / 1.00 | yes | 540.8 ms |

Additional unit checks deliberately create disagreements to verify that route mode uses Jev,
shadow mode retains the offline result, and low confidence preserves the fallback while both
recommendations remain visible. Disabled routing produces no automatic decision logs. Viewer
checks cover older records, rotated files, exact-ID filtering, malformed lines and terminal
control-character escaping. Content-off checks verify matched learned terms are omitted.

## Enabled installed-hook verification

After explicit approval, the local installation was enabled with `routing.enabled: true`,
`jev.enabled: true`, `jev.mode: route`, and `jev.log_content: true`. A pre-activation config
backup was retained. The real installed hook scripts were then invoked as subprocesses with
synthetic hook payloads, reading the persisted config and saved credential.

| Hook input | Offline / Jev / final model | Verified behavior | Latency |
|---|---|---|---|
| UserPromptSubmit: lookup | Sonnet / Sonnet / Sonnet | No delegation directive | 611.3 ms |
| UserPromptSubmit: endpoint validation | Opus / Opus / Opus | Names `mid-task-opus` | 590.8 ms |
| UserPromptSubmit: migration design | Fable / Fable / Fable | Names `heavy-task-fable` | 531.9 ms |
| PreToolUse Task: generic agent with migration task | Fable / Fable / Fable | Rewrites to `heavy-task-fable` | 568.0 ms |

All four Jev calls returned HTTP 200. Both evaluator results were present, the learned classifier
was loaded, credential redaction held, and the installed status command confirmed routing and
content logging were on. This verifies hook execution and emitted directives. Actual Claude
delegation and downstream model access still require an end-to-end local Claude Code session.

The tests cover each tier, confidence fallback, shadow evaluation, two-tier menus, command and
subagent exclusions, invalid configuration and responses, HTTP errors, credential redaction,
private rotating logs, project opt-out, hook integration, standalone installation and uninstall.
A real subprocess test verifies deadline termination; HTTP contract tests use mocked responses.

## Live viewer verification

The installed `logs --follow --json` viewer was started before invoking the installed
UserPromptSubmit hook with a synthetic Python list-versus-tuple question. The viewer emitted
the offline Sonnet recommendation and `jev.status: evaluating` while the hook subprocess was
still running. It then emitted the matching result: HTTP 200, Jev Sonnet, final Sonnet,
650.1 ms. The hook exited successfully without a delegation directive, and the saved credential
was absent from the log. This verifies visibility before evaluation completes, not just a
snapshot of the finished log.

Tests cover missing files appearing later, incomplete records, rotation and truncation,
request-ID filtering, pending requests at startup, history limits, idle polling without file
rereads, and clean Ctrl+C termination. A real viewer subprocess also proves output is flushed
before a response is written, including when stdout is a pipe. Content-off tests check that
the newly added pre-call offline snapshot preserves the existing privacy setting.

## In-session notice verification

Enabled `routing.show_decisions` in the local installation and upgraded its owned hooks with
`statusMessage: Evaluating model routing`. Ran a synthetic prompt through Claude Code 2.1.284
in print mode with tools disabled, no persisted conversation, and the installed prompt hook.
Claude emitted a `system` / `informational` event at `notice` level containing:

```text
UserPromptSubmit says: [model-switcher] Offline: sonnet (0/10) | Jev: sonnet (100%, 851.0 ms; accepted) | Selected: sonnet via Jev (agree)
```

The Claude request completed successfully with `OK`. This verifies that the hook's
`systemMessage` reaches Claude's user-notification channel, separate from model context. The
interactive spinner was configured but not visually tested in the user's iTerm2 window.
Unit tests also verify simple routes, disagreements, shadow mode, low confidence, errors,
disabled Jev, opt-out, excluded prompts, agent calls left unchanged, terminal escaping, and
display failures that leave routing intact. Lifecycle tests verify notices from the installed
layout; settings tests verify upgrades preserve unrelated hooks and custom spinner text.

## In-session request/response verification

Enabled `routing.show_exchange` with the existing explicit content-logging setting. A further
synthetic Claude Code request (tools disabled, no persisted conversation) emitted a 2,870-character
user notice containing both the exact outgoing Jev request and its HTTP 200 response. Verified
the prompt, `questions.routing_tier`, response `answers`, request ID, and absence of the saved
credential. Jev selected Sonnet with 1.0 confidence in 596.1 ms; Claude completed with `OK`.

Tests verify that the displayed JSON matches the actual request/response objects, credentials
and control characters are handled safely, body previews fit Claude's 10,000-character limit,
complete content remains in the log, content-off settings suppress body display, and project
overrides cannot turn the display on. Failure checks distinguish unsent requests from calls
with no response body. Agent hooks use the same display path without adding bodies to model
context.

## Display switch verification

Added `model-switcher display --details` and `--no-details`, plus read-only inspection with no
flag. Tests verify that toggling changes the next hook notice, preserves routing and content
logging preferences, keeps file permissions, and leaves the original config intact on a failed
atomic replacement. Missing or malformed configs are rejected. Enabling details requires the
existing content-logging opt-in; it cannot silently turn on external evaluation or logging.
The install lifecycle test invokes the switch from the standalone installed CLI.

These checks verify integration behavior. They do not establish general routing accuracy,
model availability in a Claude account, or realized cost savings. Use the live smoke command
and the evaluation plan in [the research analysis](jev-research.md) to measure those.
The CI Python/OS matrix and CI shellcheck have not been run locally.
