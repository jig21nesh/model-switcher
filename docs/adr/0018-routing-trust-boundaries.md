# ADR-0018: User-owned routing policy and explicit external-evaluation scope

Status: accepted (2026-09-30). Supersedes the project authority in ADR-0003 and narrows the
automatic Jev opt-in in ADR-0016. ADR-0015's disabled-state guarantee remains authoritative.

## Context

The v0.4.0 project merge validated several field types but copied unknown routing fields too.
A repository could re-enable globally disabled routing, alter thresholds and generic-agent
membership, or resume Jev calls when global Jev remained enabled. The agent hook also emitted
`permissionDecision: allow`, unnecessarily coupling a routing rewrite to tool approval.

Jev's prompt may be user text or a delegated task containing context copied by Claude from
files and earlier turns. Local credential redaction did not protect the outbound body.
See the [baseline assessment](../security-impact-analysis-2026-09-30.md).

## Decision

- Treat repository configuration as opt-out-only. Accept only literal `false` for
  `routing.enabled`, `routing.agents`, `routing.show_decisions`, `routing.show_exchange`,
  `jev.enabled`, `jev.evaluate_agents`, and `jev.log_content`. Ignore everything else.
  Never replace an invalid global section with an enabled default during this merge.
- Keep optional threshold/tier tuning in `project_settings` inside the user-owned global
  config. Keys are exact canonical directory paths; values may set only `routing.tiers`,
  `complexity.threshold`, and `complexity.standard_threshold`. Apply repository opt-outs last.
  Neither this map nor repository files can reverse a global routing/agent opt-out.
- Read project overrides through pinned directory/file descriptors, reject symlinks and
  non-regular files, and cap the actual read at 64 KiB plus a sentinel byte. This is a stdlib
  POSIX implementation for the supported Linux/macOS environments. Ignore invalid files.
- Omit tool permission decisions. Preserve the complete input except the routed subagent
  name; let Claude evaluate permissions normally. Recognize both `Agent` and legacy `Task`.
  Migrate only the installer's owned hook to the exact `^(Agent|Task)$` matcher, retaining
  its custom hook options and leaving other hooks' matchers intact.
- Require automatic Jev scope. Default `scope: projects` with empty `allowed_projects`
  makes no automatic calls. Each allowed entry is an exact canonical directory path (maximum
  256); no implicit descendants or worktrees. `scope: all` is an explicit global choice.
  `evaluate_agents: true` separately permits delegated text. Missing or invalid consent
  retains offline routing, even in shadow mode. No credential is loaded for denied consent.
- Preserve the explicit CLI smoke command as consent for one evaluation, independent of
  automatic project scope. It does not persist consent. `--offline` previews only.
- Check bounded outbound prompt text for the configured credential, bearer tokens, recognized
  token prefixes, and private-key headers. On a match use `sensitive_content` and retain the
  offline choice. The rejected text and matched scoring terms are never logged/displayed.
  This cannot reliably detect arbitrary sensitive prose or personal data.
- Record effective routing-policy source in offline metadata. Record Jev consent and
  `transmission: attempted/not_sent` where applicable. An attempted send does not establish
  receipt, and a timeout does not establish that data stayed local. Only sent requests can
  have content logs. User notices still show fallback reasons without rejected content.

Anthropic documents input modification without an approval decision in the
[SDK hook guide](https://code.claude.com/docs/en/agent-sdk/hooks#modify-tool-input).
The [permission reference](https://code.claude.com/docs/en/permissions#extend-permissions-with-hooks)
also clarifies that hook `allow` does not override matching deny/ask rules. Removing the field
avoids auto-approval; it does not claim to fix a demonstrated universal permission bypass.

## Consequences and migration

This intentionally narrows existing behaviour. Repository thresholds/tier changes stop taking
effect; users move them into `project_settings`. "Global off, repository on" is removed.
Old `jev.enabled: true` installations without scope settings fall back offline until the user
chooses a scope; delegated evaluation additionally needs its own opt-in. No installer rewrites
user consent or deletes saved logs/credentials.

Paths use the physical working directory, not a Git-root search. Starting below an approved
root stays offline unless the subdirectory is separately approved. With explicit `scope: all`,
an opt-out only at the repository root does not protect sessions started in descendants; users
should choose project scope for sensitive work. Status reports the policy for the current
directory. Existing analysis commands (`explain`, `tune`, `tiers`) remain global/offline.

The independent local classifier, Jev comparison logs, three configured model choices,
timeouts, response validation, specialist exclusions and separate display opt-in remain.
Tests cover hostile overrides, sensitive-content suppression, both tool names, scope/origin
consent, bounded project reads and temporary-install upgrades. Real Claude approval behaviour
must be interpreted against the supported Claude version and its permission configuration.

## Verification

On macOS with Python 3.14.5, the updated full suite passes 1,151 tests, including temporary
installation/upgrade/uninstall checks. All 16 measured runtime files pass the 80% line and
branch coverage floor. Ruff 0.14.2, shell syntax, whitespace checks, documentation links and
the README's nine JSON examples pass validation. Security probes use synthetic credentials
and mocked transport. The CI operating-system/Python matrix and a live interactive Claude
permission-prompt test have not been run for this branch.
