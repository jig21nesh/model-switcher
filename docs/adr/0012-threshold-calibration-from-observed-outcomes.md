# ADR-0012: Calibrate complexity.threshold against observed outcomes

Status: accepted (2026-07-25)

Extends [ADR-0006](0006-learned-routing-weights-from-observed-outcomes.md), which established that
"did this prompt become real work?" is observable from a transcript. That decision was applied to
term weights. This applies the same evidence to the one number the user is actually asked to set.

## Context

`complexity.threshold` is the single knob that decides what gets delegated, and it ships as `5`
because a number was needed. Nothing measured it. The README's advice — "raise it if too much gets
delegated, lower it for more heavy-model routing" — asks the user to tune a number by feel, in a
loop whose feedback arrives days later and is confounded by whatever they happened to be working
on. Two questions had no answer anywhere in the tool:

- At a given score, how often does a prompt at that score actually turn into work? Without this,
  the threshold is a guess about a distribution nobody has looked at.
- What does moving the threshold cost? Delegation is the expensive direction, and the trade against
  precision and recall was invisible.

`learn` already answers the first question implicitly — it computes precision and recall at *one*
threshold as a before/after check on its own weights — but it never shows the distribution and
never sweeps. The evidence was being collected and thrown away.

## Decision

- **A `tune` subcommand prints two tables and nothing else it cannot support.** A calibration table
  (per score 0–10: prompts, and the share that became real work) and a threshold sweep (per
  candidate: delegated share, precision, recall, F1, estimated cost per 1000 prompts). Both mark
  the threshold currently in force.
- **The labelling is `learn`'s, imported, not reimplemented.** `analyze_history.trainable_turns`
  supplies both the filtering and the heavy/light label. Two commands answering "did this become
  work?" differently would make both untrustworthy, and the label's known weakness (ADR-0006: a
  hard question answered in two tool calls counts as light) then applies identically to both.
- **Scoring goes through the router, learned classifier included.** The table has to describe the
  routing that is installed, not a built-in-only approximation of it, or the threshold it justifies
  would be the right threshold for a different scorer.
- **The cost column re-prices each prompt's own recorded tokens at the tier it would route to**
  (`models.simple` in-session, `models.complex` when delegated), using `cost_statusline`'s
  `usage_cost`/`usable_pricing`/`resolve_pricing`. Pricing maths lives in one place; a second
  implementation would drift, and cache-TTL and fast-mode rates are exactly where it would drift.
- **The cost column is labelled an estimate wherever it appears, and is dropped rather than
  guessed.** No usable pricing table, an unset tier, a model with no rates, or a corpus that
  recorded no usage at all: the column disappears and the reason is printed. A table of zeroes
  reads like an answer.
- **A recommendation is printed only when the corpus earns one.** Below
  `MIN_TRAINABLE_TURNS` (150, the same bar `learn` uses), or with every prompt carrying the same
  label, or when no candidate beats the current threshold by `MIN_F1_GAIN` (0.02 F1), the output
  says so plainly instead of naming a number. Ties resolve toward the threshold already in use.
- **A recommendation that moves the delegated share states its price.** F1 weights a wasted
  delegation and a missed one equally; the user's wallet does not. When the sweep is priced, the
  recommendation quotes both estimates, so "delegate more" is never suggested without the number
  that argues against it.
- **`analyze_history.iter_turns` now keeps each turn's raw usage blocks**, keyed by message id so a
  streamed message counts once — the same dedup rule `cost_statusline.parse_transcript` uses.
  Nothing in `learn` reads them; only `tune` re-prices them.

## Consequences

- The cost column is a counterfactual and inherits the flaw ADR-0008 named for `saved`: it assumes
  the same prompt burns the same tokens on either tier. It does not — a stronger model may finish
  in fewer turns, or think for longer. Unlike `saved`, the error here has no known direction, so it
  cannot be argued to be conservative. It is stated as an estimate in the output and in the README,
  and is deliberately given per 1000 prompts rather than as a total, so it reads as a rate to
  compare across rows rather than as a bill.
- A middle tier is not modelled. A three-tier install prices the sweep as in-session versus
  `models.complex` and says so in the output. Sweeping two thresholds against each other would need
  a two-dimensional table, and the middle tier's own edge (`standard_threshold`) is a separate
  question from the one this command answers.
- `tune` reads the whole corpus and is slow. Like `learn`, it never runs on the interactive path
  and changes nothing: it prints, and the user edits `config.json` themselves. Nothing here writes
  to the config, so a bad recommendation cannot take effect without a human acting on it.
- Turn dicts gained a `usage` key. Callers that build turn dicts by hand (including `learn`'s own
  tests) do not set it, so anything reading it must tolerate its absence — `price_turns` does, and
  reports the corpus as unpriceable rather than as free.
- The calibration table is the more durable half. Precision and recall depend on the label; the
  distribution of scores and what happened at each one is closer to raw observation, and it is what
  makes a threshold arguable at all.
