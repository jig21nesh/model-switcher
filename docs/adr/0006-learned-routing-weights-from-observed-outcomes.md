# ADR-0006: Learn routing weights from observed outcomes

Status: accepted (2026-07-25)

## Context

The scorer is a hand-written keyword list (`hooks/complexity_router.py`). It has no way to improve,
it encodes one person's guess about which words imply work, and it is identical for a Terraform
monorepo and a hobby React app.

Measured against a real local corpus — 2,124 usable prompts across 101 sessions, labelled by what
each prompt actually caused — the built-in scorer at threshold 5 achieves **33.0% precision and
34.3% recall**. Two thirds of delegations pay heavy-model rates for prompts that resolved in one or
two tool calls, and two thirds of the work that did need the heavy model never got there.

The signal needed to do better is already on disk. Claude Code transcripts record, per turn, the
user's prompt and every tool call that followed it. Whether a prompt *became* work is therefore
observable rather than guessed: tool calls, file edits and spawned subagents are all in the record.

An earlier attempt to measure this produced nonsense — "score 0 → 54% heavy" — because it credited
a whole turn's work to whatever prompt preceded it. A one-word "go ahead" inherits work specified
several turns earlier. That is a property of conversation, not a scoring failure, and any labelling
scheme has to handle it.

## Decision

- **Label from actions, not words.** A turn is heavy if it spawned a subagent, made 3+ file
  mutations, or made 12+ tool calls. Output tokens are deliberately excluded: thinking models
  inflate them, and verbosity is not work.
- **Exclude prompts that cannot be judged on their own.** Short affirmations ("yes", "go ahead")
  inherit earlier context; slash commands and command echoes are never scored by the router
  anyway; turns with no observable outcome carry no label. Roughly a quarter of prompts are
  dropped, and the remainder are the ones a scorer could actually get right.
- **Weight terms by smoothed log-odds, shrunk by evidence.** Raw log-odds pins any term that
  happens to appear only in heavy prompts to the maximum weight — the first run produced "acer"
  and "accordingly" at the ceiling. Multiplying by `n / (n + 40)` and requiring 10+ occurrences
  across 3+ distinct sessions removes that entirely: on the same corpus no term now reaches the
  clip.
- **Emit a portable JSON artifact, not Python.** `docs/classifier-schema.md` specifies the format
  and the exact tokenization; `config/classifier.schema.json` makes it machine-checkable. Applying
  it requires a JSON parser and a string split, so Codex CLI, Gemini CLI, opencode or a bespoke
  harness can consume the same file.
- **Review, then promote.** `learn` writes `classifier.candidate.json` and prints a before/after
  accuracy comparison measured on the user's own history. `--apply` promotes it. Routing does not
  change until the user says so.
- **Refuse to guess from thin evidence.** Below 150 usable prompts, or when no term earns a weight,
  the tool writes nothing and says why.

## Privacy

The artifact is derived from prompt text, so it is a new place secrets could come to rest. Terms
must survive every one of: 3+ distinct sessions, 10+ occurrences, 3–24 characters starting with a
letter, at most one hyphen, and not ≥12 characters containing a digit. The file is written `0600`,
and no prompt text, full-prompt hash or file path is ever stored.

These are not hypothetical. The first run on a real corpus emitted `jiggys-macbook-pro` — a machine
hostname pasted in from terminal output. The at-most-one-hyphen rule removes it at no measurable
cost to accuracy. A common-word list was added for the same reason: without it the table filled
with "some", "want" and "will", which track one person's phrasing rather than the work implied.

## Consequences

- On the corpus above, precision moves from 33.0% to 41.9% and recall from 34.3% to 42.4% — about
  a quarter fewer wasted delegations and a quarter fewer missed ones. The tool reports this
  comparison for each user's own data rather than asking them to trust a number from elsewhere.
- **The label is a proxy.** A genuinely hard question answered correctly in two tool calls is
  labelled light. The weights optimise for "became work", which correlates with "needed the better
  model" but is not identical to it. This is the main honest limitation, and the reason the ceiling
  here is well short of perfect.
- Weights are personal by construction. That is the point — they adapt to one person's projects and
  vocabulary — but it means they do not transfer, and a shared artifact would carry one team's
  idiolect into another's routing.
- The common-word list is a tuned heuristic, not a principled linguistic object. Request-shaped
  verbs like "need" and "ensure" measurably predict work and are deliberately kept, while "want"
  and "will" are excluded. That asymmetry is empirical and is documented in the source.
- A new artifact must be produced periodically to stay current, and stale weights degrade quietly
  rather than loudly. `generated_at` exists so the staleness is visible.
- The scoring path stays offline and deterministic: this tool never runs during a session, and the
  artifact it produces is read but never written by the router (ADR-0007).
