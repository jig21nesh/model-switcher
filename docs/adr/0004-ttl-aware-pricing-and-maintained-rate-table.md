# ADR-0004: TTL-aware cache pricing and a maintained rate table

Status: accepted (2026-07-25)

## Context

The cost statusline priced every cache-creation token at a single `cache_write` rate. That rate is
documented as 1.25x input, which is the **5-minute** TTL price; 1-hour cache writes cost **2x**
input. The two are not interchangeable, and Claude Code leans heavily on 1-hour caching.

Measured across a real local corpus of 3,221 transcripts: 42,295 of 42,771 assistant records used
1-hour cache writes and only 334 used 5-minute. Pricing all of them at 1.25x under-reported
cache-write cost by roughly 60% — about $1,766 on that corpus. Cost tracking is the feature users
rely on to decide whether routing is paying for itself, so a 60% error in the dominant term is not
a rounding issue.

Three smaller problems surfaced alongside it:

- The transcript already carries `usage.cache_creation.ephemeral_{5m,1h}_input_tokens`, so the
  information needed to price correctly was present and simply unused.
- A small number of entries (27 in the corpus) carry a `cache_creation_input_tokens` total that
  disagrees with the breakdown beside it — in one case the breakdown is *larger* than the total, so
  any "total minus breakdown = remainder" arithmetic goes negative.
- `usage.speed == "fast"` marks fast-mode turns, which run the same model at premium rates
  (Claude Opus 5 fast mode is $10/$50 against a $5/$25 standard rate). Pricing those at the
  standard rate under-reports by 2x.
- Rate validation accepted any `int`/`float`, so `"input": true` priced tokens at $1/MTok and a
  negative rate produced negative cost. `bool` is a subclass of `int`; the router already guards
  against exactly this for `complexity.threshold`.

Separately, the shipped `config/config.example.json` had drifted: it had no entry for
`claude-opus-5` despite that model already appearing in transcripts, so those turns rendered as
`no rate: claude-opus-5` and were silently excluded from the session total.

## Decision

- **Price cache writes by TTL.** Add an optional `cache_write_1h` rate per model. The statusline
  reads the per-TTL breakdown from the transcript and prices each bucket at its own rate.
- **Treat the breakdown as authoritative when present.** Where a breakdown exists, ignore
  `cache_creation_input_tokens` entirely rather than reconciling the two. This handles the
  inconsistent entries without special-casing them and cannot produce a negative bucket. Fall back
  to the flat total only when no breakdown is present.
- **Add an optional per-model `fast` rate block**, selected when a transcript entry reports
  `usage.speed == "fast"`.
- **Every new key is optional and absence reproduces the previous behaviour exactly**: no
  `cache_write_1h` prices 1-hour writes at `cache_write` as before, and no `fast` block prices fast
  turns at standard rates as before. Existing configs keep working untouched.
- **Validate rates properly**: reject `bool`, non-finite, negative, and implausibly large values.
  An unusable optional rate is dropped while the model's base rates are kept, so one bad key does
  not silently remove a model from cost tracking.
- **Ship a maintained rate table** at `config/pricing.json` (schema-versioned) and a user-invoked
  `scripts/update_pricing.py` (`./bin/model-switcher pricing`) that fetches it over HTTPS,
  validates every rate before writing, shows a diff, backs the config up, and applies only with
  `--yes`. `--offline` uses the copy bundled in the repo.

## Consequences

- Reported session and turn costs go **up** for anyone using 1-hour caching. This is a correction,
  not a regression, and it should be called out in release notes so users do not read it as the
  tool becoming more expensive.
- A CI test asserts `config/config.example.json` and `config/pricing.json` agree, so the starter
  config can no longer ship stale.
- The project gains its first network call. It is confined to a user-invoked maintenance command:
  the hook and the statusline still make none, and the scoring path remains offline and
  deterministic. HTTPS is required, the response is size-capped, and nothing is written until the
  whole table validates — a malformed or hostile table fails closed, leaving the config untouched.
- `update_pricing.py` prints rate values to stdout, which is the point of a pricing diff. The
  project rule against logging pricing values continues to apply to the hook and statusline, whose
  stderr output must never carry config contents.
- Fast-mode rates are only published for Claude Opus 5, so that is the only model shipping a `fast`
  block. Others fall back to standard rates until a documented rate exists.
- Keeping models the source table does not know means a user's hand-added or private model entries
  survive an update.
