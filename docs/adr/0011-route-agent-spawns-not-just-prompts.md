# ADR-0011: Route agent spawns, not just prompts

Status: accepted (2026-07-25)

## Context

The routing policy was only ever applied to what the user typed. `UserPromptSubmit` fires on
prompts; agent prompts are sidechains and never reach it. So the policy — "work at this level
must run on the configured tier" — held for the first hop and was silently abandoned at the
second, which is where much of the work actually happens.

The scale of that gap is measurable. Across the local corpus, agent transcripts hold 44.5% of all
input tokens and 73% of all output tokens (ADR-0010). In the twenty most recent sessions, `Task`
was called with `general-purpose` 72 times and with a configured tier agent once. `general-purpose`
has no model of its own — it inherits the session model — so nearly every delegated task was
running on whatever the session happened to be, entirely outside the routing ladder.

Two options were considered and rejected:

- **Fan-out as a tier** — let a COMPLEX prompt authorise N parallel heavy agents. This changes the
  directive's meaning and multiplies spend for a benefit that has not been demonstrated.
- **Annotation only** — allow every spawn but attach a note about cost. Visible, but it changes
  nothing, and a policy that only comments is not a policy.

## Decision

Add a `PreToolUse` hook matched on `Task` (`hooks/agent_router.py`) that scores the delegated
prompt with the same scorer and rewrites `subagent_type` via `updatedInput`.

Deliberately narrow, because rewriting another agent's tool call is intrusive:

- **Upgrade only, never downgrade.** A caller that named a specific agent knows something the
  score does not. Only agents with no model or speciality of their own are eligible — by default
  `general-purpose` and `claude`, configurable via `routing.generic_agents`.
- **`Explore` is excluded on purpose.** It is a cheap read-only search agent; promoting it to the
  heavy tier would spend a lot to do very little.
- **Top-level spawns only.** When `agent_id` is present the call came from inside an agent, and
  promoting there would let a tier agent escalate its own helpers a level down where nobody is
  watching.
- **Never rewrite to an agent that does not exist**, which would turn a working call into a
  failing one.
- **Never silent.** Every rewrite carries `additionalContext` naming the score, the threshold, the
  agent chosen and how to switch the behaviour off.
- Tied to the same switch as prompt routing: off whenever `routing.enabled` is false, and
  separately disableable with `routing.agents: false`.
- Fails open exactly like the prompt router: any error exits 0 with no output and the call runs
  as Claude wrote it.

## Consequences

- The routing ladder now applies to delegated work, so a three-tier config genuinely has three
  tiers rather than three tiers for prompts and one for everything Claude does next.
- Cost moves in whichever direction the config implies. Promoting `general-purpose` onto the heavy
  tier raises spend when the session model is cheaper than `models.complex`, and lowers it when
  the session model is dearer. This is the same trade the prompt router already makes; it is now
  simply applied consistently. ADR-0010 means the result is at least visible.
- **A rewrite can route work onto a model with no available quota.** That is not detectable
  offline — the agent file exists and the config is valid — so the hook cannot guard against it.
  `model-switcher status` reports failed delegations with their reason, and `routing.agents: false`
  disables the behaviour.
- Adds a second hook to `settings.json`. The installer registers both and uninstall removes both;
  a lifecycle test asserts the file is restored byte-for-byte.
- The hook only runs when Claude spawns an agent, not on every tool call, because the matcher
  narrows it to `Task`.
