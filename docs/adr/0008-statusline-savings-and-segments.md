# ADR-0008: Statusline savings and configurable segments

Status: accepted (2026-07-25)

## Context

The statusline reported what a session cost but never whether routing was worth having. A user
could see `session $4.23` and had no way to tell whether that was a good outcome, a bad one, or
identical to doing nothing. The project's central claim — that routing saves money — was the one
thing the tool did not measure.

Separately, the line was a fixed string. Every added fact (savings, tier count, routing state)
would make it longer for everyone, including users who wanted the terse original.

## Decision

- **Report savings as an explicit counterfactual.** Re-price every transcript entry at the
  `models.complex` rates and subtract actual spend. Render it as `saved $8.13 (66%)`, and describe
  it in the README as "what routing avoided", never as a bill.
- **Stay silent when there is nothing to report.** No savings, no segment — including when routing
  is disabled, where a truthful `$0.00` would just be noise. The same rule applies to `routing`,
  which renders only when the state is notable (off, or three tiers) rather than restating the
  default on every refresh.
- **Resolve the baseline model from the alias, preferring the shortest matching key.** Config names
  models by alias (`fable`); pricing is keyed by id (`claude-fable-5`). Base ids are shorter than
  dated variants, so `sonnet` resolves to `claude-sonnet-5` rather than `claude-sonnet-4-6`. Where
  that also picks the cheaper entry it *understates* savings, which is the right direction to be
  wrong in for a number that flatters the tool. `statusline.savings_baseline` overrides it.
- **Make the line a list of segments** (`statusline.segments`), rendered in the configured order,
  with unknown names dropped and warned about rather than breaking the line.

## Consequences

- The tool now measures its own value, and can therefore show that it is *not* delivering any —
  a three-tier setup with a badly chosen middle model shows little or no saving, which is exactly
  the feedback ADR-0005 said was missing.
- The default line is longer than before. Segments that self-silence keep it near the old length in
  the common case, and anyone who wants the original can set
  `"segments": ["turn", "session", "tokens"]`.
- The savings figure inherits every limitation of the cost estimate it is built on: transcript
  tokens and a user-maintained pricing table, not billing data. It also assumes the counterfactual
  session would have consumed identical tokens, which is not strictly true — a stronger model may
  solve the same task in fewer turns. It is directional, not exact, and the README says so.
- Sessions whose transcripts contain a model with no configured rate exclude those entries from
  both actual and baseline, so the percentage stays consistent even while `no rate:` is displayed.
- Segment names are user-supplied config and may not be strings, or even hashable. A hostile-input
  test caught a `TypeError` on an unhashable value that would have degraded the statusline to its
  error line; membership is now type-guarded first.
