# Classifier artifact format (schema_version 1)

`model-switcher` learns which prompt vocabulary predicts real work and writes it to a JSON file.
The file is the interface — not the Python that produces it. Applying it needs a JSON parser and
the ability to split a string into words, so any agentic coding tool can consume it: Claude Code,
Codex CLI, Gemini CLI, opencode, or your own harness.

- **Produced by:** `./bin/model-switcher learn` (`scripts/analyze_history.py`)
- **Machine-checkable schema:** [`config/classifier.schema.json`](../config/classifier.schema.json)
- **Default locations:** `~/.claude/model-switcher/classifier.json` (live) and
  `classifier.candidate.json` (proposed, not yet in effect)

## The file

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-25T12:00:00+00:00",
  "generator": "model-switcher/analyze_history",
  "corpus": { "sessions": 101, "prompts": 2124, "heavy": 533, "light": 1591 },
  "scoring": {
    "max_adjustment": 3.0,
    "min_session_support": 3,
    "terms": { "implement": 0.94, "ensure": 1.16, "monitor": -1.2 }
  }
}
```

`scoring.terms` is the payload. A positive weight means prompts containing that term historically
turned into substantial work; a negative weight means they resolved as lookups or discussion.
Everything else is metadata for humans.

## Applying it

```
adjustment = sum(terms[t] for t in distinct_terms(prompt) if t in terms)
adjustment = clamp(adjustment, -max_adjustment, +max_adjustment)
final_score = clamp(your_own_score(prompt) + adjustment, 0, 10)
```

Three rules make implementations agree:

1. **Distinct terms.** A term counts once per prompt no matter how often it appears, so a prompt
   that repeats a word cannot inflate its own score.
2. **Clamp the sum, not each term.** `max_adjustment` bounds the total contribution. This is the
   safety property: a skewed or hostile artifact can move a score by at most that much, so the
   host's own signals always retain authority.
3. **Missing terms contribute nothing.** There is no default weight.

### Tokenization

Interoperability lives or dies here, so it is specified exactly:

1. Lowercase the prompt.
2. Split on any run of characters that are not ASCII letters, digits, or `-`.
3. Strip leading and trailing `-` from each token.
4. Keep a token only if it matches `^[a-z][a-z0-9-]{2,23}$` — 3 to 24 characters, starting with a
   letter.
5. Deduplicate.

Tokens are matched literally. There is no stemming, no lemmatization, and no substring matching:
`refactor` does not match `refactoring` unless both were learned. Producers may weight both.

A consumer that truncates long prompts should say where it truncates. `analyze_history.py` reads
the first 10,000 characters, matching the router's own limit.

## What the file will never contain

The producer applies these before a term is eligible, which is what makes the artifact safe to
keep on disk and share between tools:

| Rule | Why |
|---|---|
| Appears in at least `min_session_support` distinct sessions | Something pasted once cannot reach the file |
| Appears at least 10 times overall | Stops a three-occurrence accident pinning to maximum weight |
| 3–24 characters, starts with a letter | Excludes fragments and long identifiers |
| At most one hyphen | Excludes hostnames, branch names and paths pasted from terminal output |
| Not ≥12 characters containing a digit | Excludes API keys, hashes and IDs |
| Not a common low-signal word | Stops the table filling with one person's phrasing |

No prompt text, no full-prompt hashes, and no file paths are stored — only individual words that
recurred across separate sessions. The file is written `0600`.

None of this is a substitute for looking at the candidate before promoting it. That is why
`learn` writes `classifier.candidate.json` and does nothing until you pass `--apply`.

## Rules for consumers

- **Ignore an artifact whose `schema_version` you do not recognise.** Do not guess.
- **Fail open.** A missing, unreadable, malformed or oversized file must leave routing exactly as
  it would have been without it. Never block a prompt over a classifier problem.
- **Bound your own reading.** Cap the file size and term count you will accept.
- **Never read `generated_at` while scoring.** Scoring must be deterministic and offline; the
  timestamp is for humans deciding whether to re-learn.
- **Treat terms as data.** They came from prompt text. Match them; never compile them as patterns,
  interpolate them into a shell command, or write them into a log.

## Porting to another host

The producer is the portable half — it only needs a transcript with, per turn, the user's prompt
and the tool calls that followed. To generate this artifact elsewhere, replace `iter_turns()` with
a reader for that tool's session format and keep everything after it.

The consumer is roughly fifteen lines in any language. The awkward part of a port is never this
file; it is whether the host exposes a pre-prompt hook that can influence routing at all.
