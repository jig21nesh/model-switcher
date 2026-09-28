# ADR-0017: Preserve offline and Jev results in each routing trace

## Context

ADR-0016 retained the offline score inside the final decision but replaced its tier when Jev
won. A user could see the chosen tier without knowing whether the offline classifier agreed,
which model it recommended, or how its built-in and learned contributions produced the score.
Inspecting raw JSON also made routine troubleshooting difficult.

## Decision

Compute offline scoring and its explanation once, using the same `analyse_prompt` result for
routing and logging. Pass that snapshot into Jev evaluation without including it in the remote
request. Preserve the offline tier/model independently of Jev's tier/model. Each result records
both, their agreement, policy, and the final source/reason/model. Low-confidence and shadow
recommendations remain visible; errors use explicit status codes, not invented model choices.

Metadata records are written for every eligible routing evaluation, including offline-only
routing with Jev marked disabled. Disabled routing does not evaluate or log. Existing exclusions
for commands, nested agents and deliberate specialist choices remain in force. Detailed prompt,
signal and learned-term content requires the existing `jev.log_content` opt-in.

Keep the existing `logs/jev.jsonl` path and add `schema_version: 2` to result records. The shared
writer retains private permissions, credential redaction and bounded rotation. Add a local-only
`model-switcher logs` report with limits, JSON output, and exact request-ID filtering. The report
handles both schema versions, incomplete lines and rotated logs; missing old baseline tiers are
reported as unknown. The installer ships the viewer so no repository checkout is required.

Write the same offline snapshot to the request event before calling Jev, with
`jev.status: evaluating`, so users can observe an evaluation while it is running. Add
`logs --follow` to print pending requests and completed decisions as they arrive, including
JSONL streaming. Poll file metadata every 100 ms and reread only changed logs; keep reads and
deduplication bounded to the current file and its rotated backup. Wait for complete newline-
terminated records and flush each output line. Keep debug output out of hook stdout, which
Claude interprets as context. The existing content opt-in applies to both event types.

Expose a compact comparison inside Claude through the documented synchronous-hook
`systemMessage` field, including simple requests with no delegation directive. Use
`statusMessage` for the running hook's spinner. Gate this presentation on
`routing.show_decisions: true` (enabled in the example config, absent/false preserves existing
quiet installs). Pass the completed event to a local observer to form the notice; do not read
the last global log record, which might belong to a different session. Display failures must
not change routing. The default notice contains metadata only and is separate from `additionalContext`.
Agent notices clarify when the evaluated call was left unchanged. Reinstall upgrades owned hook
entries with a spinner without duplicating them or changing other hooks.

Support explicit `routing.show_exchange: true` together with `jev.log_content: true` to include
the actual outbound request and received JSON response in that same notice. Pass the request
body alongside its result in memory; do not reconstruct it or duplicate it in the result log.
Redact credentials before the event reaches the display observer. Escape terminal controls and
cap each body at 3,800 displayed characters, with an explicit truncation marker and the full
request ID for log lookup. This fits within Claude's hook-message limit while preserving both
sides of the exchange. Projects may disable this display, but cannot enable it. Missing bodies
are reported explicitly. Display settings do not alter evaluation, logging or model context.

Provide `display --details` / `display --no-details` as an explicit switch for the existing
exchange flag. Enabling details also enables summary notices, and requires the existing content
logging opt-in. Disabling changes only the exchange flag. Use an atomic config replacement that
preserves unrelated settings and file permissions; the command makes no network calls.

## Consequences

Users can inspect disagreements and fallback reasons without making another API request. The
local classifier remains useful both as a comparator and fallback. The final decision still
follows one explicit policy; two recommendations do not implicitly average scores or vote.
Offline-only routing now writes metadata to disk; prompt content and learned terms remain
absent unless content logging is enabled for Jev evaluation. Logs explain selection, not whether
Claude followed a delegation directive or whether the selected model produced a good answer.
