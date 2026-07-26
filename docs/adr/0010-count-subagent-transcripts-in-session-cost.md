# ADR-0010: Count subagent transcripts in session cost

Status: accepted (2026-07-25)

## Context

The statusline priced only the file named by `transcript_path`. That was correct when Claude Code
wrote sidechain records inline into the session transcript, which is what the code was written
against — `parse_transcript` still skips `isSidechain` user records when locating the current turn,
a fossil of that layout.

Claude Code now writes every spawned agent to its own file:

```
<project>/<session-id>.jsonl              the session
<project>/<session-id>/subagents/agent-*.jsonl     each subagent
<project>/<session-id>/wf_*/agent-*.jsonl          each workflow agent
```

Measured across the local corpus (3,128 agent transcripts):

| | input tokens | output tokens |
|---|---|---|
| session transcripts | 4,810M | 18.9M |
| agent transcripts | 3,854M | 51.1M |

**44.5% of input and 73% of output was invisible to the statusline**, and a scan confirmed zero
sidechain records with usage remain inline in any session file — so the old path recovered none of
it. The README asserted in three places that subagent usage was included. It was not.

The distortion is worst exactly where accuracy matters most: a session that delegates heavily is
the one whose cost is most understated, and it is the one the tool exists to justify.

There was a second consequence. ADR-0009 made the savings figure require two priced models in a
session. Delegated work runs in an agent file, so the delegated model never appeared — meaning
`saved` could not fire for the very sessions that had actually saved something.

## Decision

- **Read `<transcript>/**/*.jsonl` alongside the session file.** The directory is named after the
  session id, so attribution is exact rather than inferred from timing or working directory.
- **Attribute agent work to the turn by timestamp**, comparing against the last real user message.
  Agent entries have no line number in the session file, so the existing line comparison cannot
  place them.
- **Where timestamps are absent, count towards the session but never the turn.** Understating a
  turn is recoverable; silently attributing an hour of agent work to whatever was typed last is not.
- An unreadable agent transcript is logged and skipped, never fatal — the statusline must always
  print a line.

## Consequences

- Session and turn costs rise, in some cases sharply. This is a correction, not a regression: the
  tokens were always billed, just never shown.
- `saved` becomes reachable in normal use. A session that keeps cheap work in-session and delegates
  hard work now genuinely spans two priced models, which is what ADR-0009 requires.
- Cost scales with agents spawned, so a fan-out of ten agents shows ten agents' worth of spend.
  That is the honest number and the main reason to have built this.
- Routing is untouched. The hook fires on `UserPromptSubmit` only, so agent prompts are never
  scored and a fan-out is not itself a routing decision — this ADR closes the accounting gap, not
  the routing one.
- The scan is bounded by one directory listing per statusline refresh, and only for sessions that
  actually spawned agents.

## Amendment (2026-07-26): materiality and routing state

Reading subagent transcripts reintroduced the fabricated savings figure ADR-0009 removed, by a
different route. A one-word probe sent to `heavy-task-fable` cost $0.21 in a $162 session — 0.128%
of spend — but it put a second priced model in the transcript, which satisfied the two-model guard.
The whole session was then re-priced at fable's rates. Fable is exactly 2x the session model, so
`saved` equalled the session cost at 50% once more, printed on the same line as `routing off`.

Two further conditions now apply before a savings figure is produced:

- **The baseline model must account for at least `MIN_BASELINE_SHARE` (5%) of session cost.**
  Incidental contact with a dearer model is not routing, and must not re-price everything else.
- **Routing must be enabled.** With routing off nothing was routed, so nothing was saved; claiming
  otherwise on the same line that says `routing off` is a contradiction the reader has to resolve.

Both guards are unconditional and apply to `statusline.savings_baseline` too. The lesson worth
recording is that the two-model rule was a proxy for "routing actually happened", and a proxy that
held only while subagent usage was invisible.

## Amendment (2026-07-26): turn attribution and double-counting guards

An adversarial audit of the merged-transcript arithmetic found four weaknesses in how entries are
attributed and deduplicated:

- **Tool results are not prompts.** Claude Code records every tool result as a `type:"user"` line.
  Treating those as turn boundaries collapsed `turn $` to roughly the cost of the final API call of
  the turn — in one measured session, 636 of 667 user records were tool results. Only messages
  containing typed text now move the boundary.
- **One API call, one charge.** Entries are deduplicated by message id across the session file and
  every subagent file, so a call recorded in two files cannot be paid for twice. Measured across
  recent sessions the overlap today is zero; the guard covers the transcript layouts not yet seen.
- **Timestamps are compared as times, not text.** `10:00:00.500Z` sorts before `10:00:00Z` as a
  string, silently dropping in-turn agent work when the two writers disagree on precision or offset
  spelling. Stamps that parse are compared as datetimes; text comparison remains only as the
  fallback for stamps that do not.
- **The no-timestamp fallback is per-prompt, not per-file.** When the *last* prompt carries no
  stamp, agent work counts toward the session but never the turn — the understate-don't-guess rule,
  now applied to exactly the prompt that defines the boundary.

The explicit `savings_baseline` override is also confined to models the session actually ran,
closing the last route by which a never-observed model could become the yardstick.
