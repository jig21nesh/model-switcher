# 15. Fail closed on ambiguous routing state

## Context

`routing.enabled` failed toward **enabled**. A missing config, a corrupt config, a `routing`
section written as a string, or `"enabled": "false"` (the string) all re-enabled routing, and two
tests asserted that direction deliberately. The reasoning was "installed means the user wants
routing", so degraded state fell back to the product's purpose.

An adversarial audit showed what that costs. The disabled state itself held — 66 of 66 checks
produced byte-for-byte silence — but the *flag protecting it* did not: one corrupt byte in
config.json, or one quoted boolean, revived the router. A user who has explicitly turned routing
off (this machine, today) is one failed write away from delegation directives reappearing in every
session. The user's own global rules say it plainly: fail closed, not open — on error, default to
denial. For a router, denial is silence.

The same audit found the disable flag's neighbours defaulting in the aggressive direction: a NaN
threshold clamped to 1.0 (route almost everything) because NaN slips through `max(1.0, min(v, 10))`,
and a NaN classifier weight clamped to +3.0 (the maximum boost).

## Decision

Routing must have a well-formed "yes" to act:

- `routing.enabled` with any non-boolean value → routing **off**, one stderr line.
- A `routing` section that is present but not an object → routing **off**.
- A config.json that **exists but cannot be parsed** (bad JSON, non-object top level, nesting
  that blows the parser) → `load_config()` returns a disabled-routing sentinel, so every consumer
  — prompt hook, agent hook, statusline savings — goes quiet together.
- A genuinely **absent** config still means "fresh install": the enabled default stands and the
  setup nag can fire. Absence is the one state with no recorded user intent to protect.
- Non-finite numbers are not numbers anywhere a number is read (thresholds, classifier weights,
  the shared `_is_number`); they fall back to defaults instead of clamping to an extreme.

Project overrides are unchanged: ADR-0003's fall-open-to-global still governs override *values*,
because there the last known good state is the global config, and that is exactly what an invalid
override falls back to. The two rules are the same rule — fall back to the nearest recorded
intent, never to the aggressive extreme.

## Consequences

- The disabled state now survives config corruption, which is the state this machine is in.
- A user who *wants* routing and corrupts their config loses it until the file parses again.
  This is the accepted cost; the failure is surfaced (stderr line on every prompt,
  `model-switcher status` reports it) rather than silently absorbed.
- The two tests that asserted fail-open now assert fail-closed; new tests cover the corrupt,
  non-object, and deeply nested config paths end to end.
- The statusline's `_routing_state` agrees with the hooks: an ambiguous state renders
  `routing off` and can never satisfy the savings gate.
