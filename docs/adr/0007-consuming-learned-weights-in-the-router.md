# ADR-0007: Consuming learned weights in the router

Status: accepted (2026-07-25)

## Context

ADR-0006 produces a learned weight table. This decides how the `UserPromptSubmit` hook reads it.

The hook is the most safety-critical code in the project: it runs on every prompt of every
session, and a failure there costs the user their prompt. The classifier is also a **new untrusted
input**. It is generated from prompt text, it lives in a file a user can hand-edit or copy from
someone else, and nothing about it can be assumed.

## Decision

- **Bounded influence, hard-coded.** The summed adjustment is clamped to ±3 of the 0–10 range by a
  constant in the router. An artifact may request a *smaller* limit through `max_adjustment`, but
  it can never raise its own ceiling — a value above the constant, or a non-number, falls back to
  the constant. Hand-tuned signals therefore always retain authority.
- **The lookup caps run last.** Short questions, definitional questions and affirmations are capped
  to 2 *after* the learned adjustment is applied, so no weight table can talk a lookup into being
  delegated. This is the ordering that makes a hostile artifact boring: the worst it can do is move
  a genuine task prompt by three points.
- **Validate every term and weight on load.** Terms must match the documented shape; weights must
  be finite non-boolean numbers; the file is size-capped and term-capped. Anything failing is
  dropped individually rather than rejecting the whole file, and an unknown `schema_version` is
  ignored outright.
- **Terms are matched, never compiled.** They are set-membership lookups over already-split tokens.
  Nothing derived from prompt text is ever turned into a regular expression, a path, or a shell
  fragment.
- **O(words) at scoring time.** No per-term regex compilation. The existing keyword lists compile
  once at import; the classifier adds a dict lookup per token, which is why a 400-term table costs
  nothing measurable on the interactive path.
- **Fail open, silently.** Missing, unreadable, malformed, oversized, wrong-version — every path
  returns "no classifier" and routing proceeds exactly as it would have without one.
- **One implementation, two views.** `analyse_prompt()` returns the score together with the signals,
  the learned adjustment, the matched terms and the caps applied; `score_prompt()` returns just the
  number and `explain` renders the rest. A separate explain implementation would drift from the
  real one, and an explanation that disagrees with the routing is worse than none.

## Consequences

- Routing decisions become explainable. `model-switcher explain "<prompt>"` shows exactly which
  signals fired, what the learned terms contributed, and where the prompt would route — without
  spending a token. This also satisfies the long-standing dry-run item on the roadmap.
- The tokenizer is duplicated between `hooks/complexity_router.py` (consumer) and
  `scripts/analyze_history.py` (producer), because the hook must stay stdlib-only and standalone
  and cannot import from `scripts/`. A test asserts every term the producer can emit is findable by
  the consumer, so the two cannot drift apart unnoticed.
- The consumer tokenizes more permissively than the producer filters. That is deliberate: the
  producer decides what deserves to be in the artifact, the consumer only needs to find it. It also
  means a hand-written artifact can use terms the learner would never have emitted.
- Scores can now be non-integral internally and are rounded once at the end. Without a classifier
  the adjustment is exactly zero, so existing behaviour is bit-for-bit unchanged — all 368
  pre-existing tests passed without modification.
- A user who edits weights by hand can degrade their own routing. The bounded adjustment limits the
  damage to three points, and `explain` makes it visible.
