# ADR-0014: Score the request, not the paste

Status: accepted (2026-07-26)

## Context

`model-switcher tune` (ADR-0012) was pointed at a real 2,186-prompt corpus for the first time and
the calibration curve was not monotonic:

```
score   prompts   became real work   avg chars
    0       991         13%              310
    4        86         49%            1,330
    7        29         52%            4,787
   10       336         40%            7,349
```

**15% of all prompts saturated at the top score, and were less likely to become real work than
prompts scoring 4.** Precision was therefore flat at 40–43% at every candidate threshold: moving
the threshold traded recall against cost and never bought accuracy.

The cause was visible in the signal mix. At score 10, `domain terms` fired on 96% of prompts,
`task verbs` on 95%, `long prompt` on 90%, and the *uncapped* base score averaged 11.6 against a
cap of 10. Those prompts averaged 7,349 characters — pasted logs, stack traces, file dumps. A paste
that large contains "test", "error", "config" and "review" whether or not the request is hard, so
every keyword signal fired at once on evidence that had nothing to do with the ask.

An earlier hypothesis — that learned terms had absorbed project vocabulary and were scoring "which
project is this" — was checked against the attribution report (ADR-0013) and **rejected**: only 5 of
283 terms come from a single project, 19 more are 90%-concentrated. Topic leakage is real but small,
and could not explain the saturation.

## Decision

**Vocabulary is read from the request; structure is read from everything.**

- Keyword lists (`STRONG_KEYWORDS`, `MODERATE_KEYWORDS`) and learned terms now score only the first
  `REQUEST_WORDS` (80) words — what a person typed, before whatever they pasted underneath.
- Structural signals still read the whole prompt: numbered steps, chained requests, code fences,
  file paths and stack traces are evidence wherever they appear.
- The `long prompt` / `medium prompt` signals are **removed**. Length was never evidence on its own;
  it was a proxy that guaranteed the keyword signals would also fire.
- `DEFAULT_THRESHOLD` moves from 5 to 3, because dropping the length points shifts the whole scale
  down. This restores the previous delegation rate at the new accuracy.

80 words was chosen by measurement, not taste. Every window tested beat full-text scoring on both
precision and recall at a matched delegation rate; 80 landed closest to the previous rate:

| window | threshold | delegated | precision | recall | F1 |
|---|---|---|---|---|---|
| full (before) | 5 | 25.6% | 42.4% | 42.3% | 42.4 |
| 40 | 2 | 29.9% | 45.9% | 53.6% | 49.5 |
| **80** | **3** | **26.7%** | **47.5%** | **49.5%** | **48.5** |
| 150 | 4 | 22.4% | 47.3% | 41.4% | 44.2 |

Comparing at a matched *threshold* rather than a matched *delegation rate* hides this entirely — at
threshold 5 the windowed scorer looks worse on F1, because it is operating at half the delegation
rate. The correct comparison is between operating points, not between thresholds.

## Consequences

- The calibration is now close to monotonic and the top bucket is the most predictive: score 10
  holds 57 prompts at 65% real work, against 336 at 40% before.
- Precision rises 42.4% → 47.5% and recall 42.3% → 49.5% at a comparable delegation rate. Both
  improve; this is not a trade.
- **Scores are lower than before for the same prompt.** Anyone with an explicit
  `complexity.threshold` in their config keeps that number and will delegate less than they used
  to. `model-switcher tune` reports what their own history now says the threshold should be.
- A huge paste with no request scores 0 rather than 2. That is the intended reading: pasting a log
  is not itself a hard task.
- Prompts where the request genuinely runs past 80 words lose the tail of their vocabulary. The
  measurement says this costs less than the noise it removes, but it is a real limitation, and a
  different corpus could put the knee elsewhere.
