# Inspect routing decisions

Every eligible prompt or generic-agent task is scored offline before Jev is consulted. With Jev
enabled and the project/origin allowed, both results are logged independently. Denied evaluation
records its reason alongside the offline result. This makes disagreements visible without changing
which policy controls the final decision.

## See evaluations inside Claude Code

Enable `routing.show_decisions: true` in `~/.claude/model-switcher/config.json`, reinstall, and
restart Claude. Fresh installs enable this display. The hook uses Claude's `statusMessage`
spinner while it runs and returns a `systemMessage` with the comparison before Claude answers.
These are the [documented hook display fields](https://code.claude.com/docs/en/hooks#json-output);
the routing directive remains separate in `additionalContext`.

Illustrative notice:

```text
[model-switcher] Offline: sonnet (0/10) | Jev: sonnet (100%, 491 ms; accepted) | Selected: sonnet via Jev (agree)
```

The summary appears for simple requests too. Agent evaluations show the same comparison and
explicitly say when the agent call was left unchanged. Shadow, low-confidence and failed Jev
evaluations show the offline selection. Turning off `show_decisions` hides these notices while
keeping the logs. The default notice shows metadata only.

For the actual request and response **in the same Claude session**, enable both
`routing.show_exchange: true` and `jev.log_content: true`. Below the comparison, the hook prints
`Jev request (POST ...)` with the exact outbound JSON (prompt, available models, question
instructions and criteria), followed by `Jev response (HTTP ...)` with the received JSON and
the full request ID. The display uses the same credential redaction as the logs and escapes
terminal control characters. It remains user-visible `systemMessage` content, separate from
the routing instructions in `additionalContext`.

Once content logging is enabled, switch the display with:

```sh
model-switcher display --details
model-switcher display --no-details
model-switcher display
```

`--details` sets `routing.show_exchange` and `routing.show_decisions` to true. `--no-details`
sets only `routing.show_exchange` to false, retaining your summary preference and logs.
Without a flag the command only reports the current exchange flag. Writes are atomic and keep
other config fields and file permissions. No command in this group invokes Jev or changes
routing, Jev enablement or content logging. `--config` selects another config file explicitly.

These flags are read on every prompt; a display change needs no session restart once the updated
hook is installed. Request and response bodies each have a 3,800-character preview limit to
keep the notice within Claude's 10,000-character limit. A truncation marker points to the local
log and request ID. A missing key or denied consent shows metadata only; a timeout shows the outbound
request and states that no response body was recorded. A project can hide the exchange but
cannot enable it. Without content logging, the notice explains that content is unavailable.
The spinner is a progress indication; full streamed request/result details remain available in
the log viewer below. Fast evaluations can finish before Claude visibly paints the spinner.

## Separate log viewer

```sh
model-switcher logs                       # last 10 decisions
model-switcher logs --follow --limit 1     # watch live in a second terminal beside Claude
model-switcher logs --limit 30            # up to 100
model-switcher logs --json                # complete request/result records
model-switcher logs --follow --json       # stream complete records as JSONL
model-switcher logs --request-id <id>     # one comparison
model-switcher logs --json --request-id <id>
```

The viewer reads the current log and its rotated backup. It never calls Jev. Long content is
abbreviated in the readable view; `--json` preserves the full stored record. Terminal control
characters are escaped. Old records remain readable, but an offline tier missing from an old
record is reported as unavailable, never inferred from Jev's chosen tier.

`--follow` (or `-f`) shows recent completed decisions and any requests still awaiting a result,
then watches for new records every 100 ms. Before each Jev API call, the hook records the
offline result and `jev.status: evaluating`. The readable view displays **Jev: evaluating...**
and **Final: pending**; the result record then shows both recommendations and the final route.
Matching request IDs connect the two entries when several sessions are active. A pending entry
means a result has not been logged yet, including if a hook was interrupted before completion.

The viewer waits if the file does not exist, follows rotation by filename, skips incomplete
lines until their newline arrives, and flushes output as it is received. `--json --follow`
outputs only JSON records, so it can feed another local tool. Ctrl+C exits cleanly. The Claude
cost statusline stays focused on model and cost; routing notices appear in the session itself.

## What a comparison looks like

Illustrative disagreement (not a claimed live result):

```text
Offline: opus (standard), score 4/10; base 3, learned 1
Jev: sonnet (simple), confidence 0.99; accepted, 450 ms
Final: sonnet via jev (accepted); evaluators DISAGREE
```

With `jev.mode: route`, Jev controls the route when its answer is valid and meets
`jev.min_confidence`. With `shadow`, the offline result controls routing. Errors, timeouts,
missing credentials and low confidence retain the offline choice. Both recommendations remain
visible even when only one is used.

`project_not_allowed`, `agent_not_enabled`, and `sensitive_content` mean Jev was not called.
These results omit prompt bodies and learned terms even when content logging is enabled.
Automatic Jev calls require user-owned `scope`/`allowed_projects`; delegated task prompts need
`evaluate_agents: true` as well. `shadow` still transmits content. The explicit `jev` CLI command
consents to one call independently of automatic scope, subject to the sensitive-content check.
See [setup and migration](../README.md#optional-jev-evaluation-before-routing).

## Fields in schema version 2

| Field | Meaning |
|---|---|
| `request_id`, `origin`, `timestamp` | Correlate request and result; distinguish user prompts, agent tasks and explicit CLI checks |
| `offline.tier`, `offline.model` | Independent offline recommendation, including explicit `simple` / `sonnet` |
| `offline.score`, `base_score`, `learned_adjustment` | Final bounded 0–10 score, built-in contribution, and learned contribution |
| `offline.classifier_loaded` | Whether a valid learned weight table contributed to evaluation (not whether a word matched) |
| `offline.thresholds`, `caps` | The thresholds in force and any lookup caps applied |
| `offline.policy` | Global or user-owned project tuning; whether repository opt-outs changed the effective config |
| `jev.transmission` | `not_sent` or `attempted`; attempted does not guarantee receipt, and a timeout does not retract a send |
| `jev.consent` | `projects`, `all`, or `explicit_cli` for evaluated requests |
| `offline.signals`, `matched_terms` | Scoring evidence; only included with content logging enabled |
| `jev.tier`, `jev.model` | Jev's independent recommendation, even in shadow mode or below the confidence gate |
| `jev.confidence`, `probabilities` | The validated decision signal, not a guarantee of correctness |
| `jev.status` | `evaluating` in the request event; `accepted`, `shadow`, `low_confidence`, `disabled`, or a specific failure/skip reason in the result |
| `jev.mode`, `min_confidence`, `evaluation_model` | Policy and requested Jev version used for this evaluation |
| `agreement` | True/false if a valid Jev answer exists; null if not comparable |
| `decision.source`, `reason`, `final_model` | Which evaluator determined the selected route, why, and the model alias |
| `prompt`, `request`, `response` | Content-logging fields: bounded prompt, outbound JSON in the request event, response JSON in the result event |
| `prompt_truncated` | Whether only the first 10,000 characters fit in the content log; oversized requests are not sent to Jev |
| `http_status`, `latency_ms` | Jev HTTP result and elapsed evaluation time, not downstream Claude generation time |

`decision.score` is the local score retained for backward compatibility; it is not a Jev score.
A null `decision.tier` means in-session/simple. `offline.tier` spells out `simple` to avoid that
ambiguity. The final route is the router's selection, not proof that Claude executed a subagent.

## When records are written

- Routing on, Jev on: `jev.request` with the offline result before the API call, then
  `jev.result` with both evaluations.
  A pre-call skip (such as a missing key) produces a result with the reason and no request event.
- Routing on, Jev off: a metadata-only `routing.decision` with the offline recommendation and
  `jev.status: disabled`. No API call or credential lookup occurs.
- Routing off: no automatic evaluations or decision logs. Explicit `model-switcher jev` still
  performs the one evaluation requested by that command.
- Slash commands, nested-agent prompts and deliberately selected specialists retain the existing
  routing exclusions. They do not produce an evaluation record.

The existing `logs/jev.jsonl` name is retained for compatibility. Files are private, bounded and
rotated. Content logging (`jev.log_content: true`) includes user text and matched learned terms;
metadata-only logging omits those. Credentials are redacted as described in the security policy.
A project can opt out of Jev; it cannot enable external evaluation or content logging. No raw
response is injected into Claude's context or used as a tool instruction.
