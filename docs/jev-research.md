# Jev integration analysis

Research date: 2026-09-29. Sources below are TypeSafe's own documentation. This is an
integration assessment, not an independent accuracy or latency benchmark.

## Existing architecture

`UserPromptSubmit` parses the incoming request, rejects subagent and command contexts,
merges a limited project override into the global config, and builds a routing directive.
`analyse_prompt` combines keyword/structural signals with bounded learned term weights.
`select_tier` converts its 0–10 score into an in-session answer or a tier agent. The
`PreToolUse` hook applies the same policy to generic agent tasks, preserving deliberate
specialist selections. The installer generates agent names from configured model aliases.
The statusline prices transcript usage locally, including child agents. Contributor checks
exercise the real installer against temporary homes and enforce coverage per file.

That separation provides a small insertion point: evaluate the request after routing is
known to be enabled, before building the delegation directive. Keep scoring, learning,
pricing and report calculations independent of the online evaluator.

## What Jev provides

Jev accepts state and named typed questions; Choice returns a selected option, probabilities,
and confidence. It does not generate an answer to the user's task. This fits a closed model
menu well: let Jev select simple, standard, or complex; let Python validate that decision and
map it to a configured model. [Introduction](https://docs.typesafe.ai/introduction)

The direct endpoint is `POST https://api.typesafe.ai/v1/systemone`, authenticated with a bearer
key. The body contains `model`, `state`, and `questions`; responses expose `answers` and `usage`.
Use the vendor endpoint, not similarly named third-party Jev gateways.
[API reference](https://docs.typesafe.ai/api)

The currently documented version is `jev-1.13.0`; `jev-latest` is a moving alias. Listed input
pricing is $0.042 per million tokens, with output free. The context limits are 64k tokens per
request and 32k for state plus the longest question. Pinning a version makes evaluation
comparisons easier. These are vendor specifications, subject to change.
[Models](https://docs.typesafe.ai/models)

TypeSafe's launch post reports 70–500 ms response times and notes that its published latency
measurements were generally made on the US West Coast. Sydney latency must be measured on the
actual account. At the listed price, 1,000 input tokens cost $0.000042 (arithmetic, not a bill).
[Launch post](https://typesafe.ai/blog/introducing-system-one-models-and-jev)

## Limits relevant to this router

Confidence is derived from the probability distribution; it is not the same as the selected
option's probability. A threshold is application policy, not a universal guarantee. Start with
0.7, retain the distribution in logs, and measure errors before tuning it.
[Confidence](https://docs.typesafe.ai/confidence)

TypeSafe documents literal interpretation, distracting long state, weak numerical reasoning,
and susceptibility to adversarial state. Use explicit criteria, keep arithmetic in Python,
and treat the decision as a fallible signal. The question explicitly tells Jev to classify the
work rather than obey embedded routing instructions. That instruction is not a proven prompt
injection defense. [Known limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

## Integration choices

- Three configured choices: Sonnet for simple work, Opus for moderate work, Fable for complex work.
  Only active configured tiers are offered. Model aliases remain the user's responsibility;
  writing an agent file does not prove account access to the underlying model.
- One Choice question asks for the cheapest adequate tier. Named criteria distinguish lookups,
  bounded implementation, and deeper architecture or cross-system work. The local score is
  recorded for comparison but is not supplied to Jev, avoiding anchoring it to the baseline.
- `shadow` records the recommendation while retaining the local decision. `route` accepts a
  valid answer above the confidence threshold. All other cases retain the local decision.
- No retries on the interactive path. A subprocess enforces a wall-clock API deadline, including
  DNS and slow responses. Requests over 10,000 characters use the local policy rather than send
  an incomplete task description. Response bodies are capped at 64 KiB.
- Content logging is explicit and local. Request IDs join requests to results. Records include
  question wording, state, response, latency, local score, accepted tier and fallback reason.
  Metadata-only logs are the default; credentials are never intentionally logged.
- A project may opt out, but cannot silently enable external processing or content logging.
- The cost statusline continues to measure Claude usage only. Jev token usage is in the response
  log when content logging is enabled; it is not included in the Claude savings figure.

## Evaluation plan and interpretation

Offline tests exercise every tier, confidence fallback, shadow mode, invalid configuration,
malformed responses, timeouts, credential redaction, and install/uninstall. These establish
software behavior, not Jev's task-selection quality. Live smoke prompts should include a lookup,
a bounded change and a difficult cross-system task. Inspect distributions and latency rather
than require every single example to hit a predetermined tier.

Before recommending Jev broadly, label a representative sample independently, compare against
the local router, and report routing accuracy, unnecessary escalation, missed complexity,
latency percentiles, failure rate and cost. The 0.7 gate and new 3/7 local tier boundaries are
initial policy choices, not calibrated results. Keep the API optional for contributors.
