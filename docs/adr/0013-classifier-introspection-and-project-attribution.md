# ADR-0013: Make the learned classifier legible, and attribute its vocabulary to projects

Status: accepted (2026-07-25)

Extends [ADR-0006](0006-learned-routing-weights-from-observed-outcomes.md) (learning weights from
observed outcomes) and [ADR-0007](0007-consuming-learned-weights-in-the-router.md) (consuming them
in the router). Neither changes: this is about looking at what they produced.

## Context

`learn` reports one thing about the table it writes: whether precision, recall and F1 improved on
the corpus it was trained on. That is the weakest evidence available — it is measured on the same
history that produced the weights, and it says nothing about *what* was learned.

Inspecting a real install made the gap concrete. 283 terms from 105 sessions and 2,143 prompts:

- **198 of 283 terms (70%) had `|weight| < 0.5`.** The scoring path rounds to whole points, so it
  takes six such terms co-occurring in one prompt to change a routing decision. Most of the table
  cannot act alone.
- **25 terms shared exactly `-0.394`**, 13 more shared `-0.444`. These are terms that met the
  minimum evidence the producer accepts (10 occurrences, 3 sessions) and nothing beyond it, so the
  arithmetic lands them all on the same value. They are numerically indistinguishable from one
  another: their ordering in any "strongest terms" list is an artefact.
- The vocabulary included `naplan`, `strava`, `colleges`, `schools`, `fitness`, `emotion`,
  `teacher` and `quiz`. These are subjects, not difficulties. A classifier that has learned them is
  partly answering "which project is this?" rather than "how hard is this?", and that answer stops
  being useful the day the operator starts a different project.

None of this is visible from an F1 score, and the third point is not visible from the artifact
alone either — the artifact deliberately contains no provenance beyond aggregate corpus counts
(ADR-0006: no prompt text, no hashes, no paths).

`explain` had a matching gap on the other side. It printed a score and a verdict but not the
*margin*: a prompt one point below the threshold and a prompt five points below both read as
"answered in-session", and there was no way to see which signal was doing the deciding.

## Decision

**1. A `classifier` command that reports the artifact, not just its existence.** Provenance, size,
weight distribution by band, the pileups where many terms share one exact weight, and the strongest
terms in each direction. The distribution and the pileup are printed whether or not they are
flattering, because those are the two facts that decide whether the table is worth keeping.

**2. Per-project attribution, recovered from the transcripts rather than the artifact.** Claude Code
stores transcripts one directory per project, so a term's provenance can be reconstructed by
re-tokenizing the prompts `learn` would have trained on and recording which project directories
matched. This keeps the artifact itself free of paths — the attribution is derived at report time,
from files the operator already has, and is never written anywhere.

Two grades are reported, because the strict claim is rare and the useful one is not:

- **one project only** — every matching prompt came from a single project.
- **90%+ from one project** — it leaked into a second project once or twice and is otherwise that
  project's word. `colleges`, `fitness` and `quiz` land here on the real corpus; only the strict
  test would have missed them.

**3. Decision-boundary sensitivity in `explain`**, printed after the verdict and before the ladder:
distance to the threshold that decided the prompt, what carried it there or what would flip it, and
what is holding it back — with topical terms marked.

**4. Two rules on how the sensitivity numbers are obtained**, because a plausible-looking suggestion
that the router would not honour is worse than none:

- **Additions are measured, not modelled.** "This term would have flipped it" is established by
  re-running `analyse_prompt` on the prompt with the candidate word in it and reading the result.
  Candidates come from the loaded classifier and the router's own keyword tables, so a suggestion
  can never name a signal that does not exist.
- **Removals go through the router's own `final_score()`.** The scoring tail (sum, cap, round,
  clamp) was extracted from `analyse_prompt` so both callers use it. A counterfactual computed by
  re-implementing that arithmetic would be free to drift from routing, which is precisely the
  failure this feature exists to prevent.

**5. `explain` samples; `classifier` reads everything.** Attribution costs a corpus pass — about
1.2s over 0.5GB. That is right for a command whose whole job is the report and wrong for one run
while composing a prompt, so `explain` reads the two newest transcripts per project and stops
tracking a term once it has appeared in two of them, then says in the output that it sampled.

## Consequences

- Routing behaviour is unchanged. Both deliverables are read-only reports; the hook, the artifact
  format and `docs/classifier-schema.md` are untouched, and the schema stays a published interop
  contract rather than a place to add provenance.
- `explain` reads transcripts where it previously read none. It is bounded, skipped entirely when
  the prompt matches no learned term or `--no-classifier` is passed, and `--transcripts` redirects
  it — which is also what keeps the tests off the developer's real `~/.claude/projects`.
- Attribution is only as good as the transcripts still on disk. Terms learned from sessions that
  have since been deleted report as attributed nowhere, and the report says how many.
- `analyse_prompt` gained no behaviour but lost its inline scoring tail. `TestFinalScore` asserts
  the two agree, so the extraction cannot silently diverge.
- The 90% concentration threshold is a judgement, not a measurement. It is stated in the output
  rather than hidden, and the strict single-project list is reported separately so the stronger
  claim stays strictly true.
- Terms are still treated as untrusted data throughout: compared as plain strings, never compiled,
  never interpolated. The report prints project *directory names*, which are paths — acceptable in
  a local report the operator asked for, and never written to a file or a log.
