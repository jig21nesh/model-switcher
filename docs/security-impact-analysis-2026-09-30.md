# Security impact analysis: routing policy, tool permissions, and Jev data sharing

Date: 2026-09-30. Baseline: `v0.4.0`, commit `35d1e6a` (current `origin/main`).
Working branch: `fix/routing-trust-boundaries`.
Status: baseline assessment, followed by implementation on the working branch. The findings below
describe v0.4.0, not the updated runtime. [ADR-0018](adr/0018-routing-trust-boundaries.md) records
the implemented policy and migration. Installed user settings have not been changed.

## Assessment

The three concerns in the supplied review warrant changes. The most consequential finding is
that a repository can turn routing back on after the user disabled it globally. If Jev remains
enabled in the user's global configuration, this also resumes external evaluation. The
existing rule that a project cannot set `jev.enabled: true` does not prevent that combination.

| Finding | Impact | Priority |
|---|---|---|
| Repository configuration can override user-owned routing controls | Unexpected model cost/selection, specialist substitution, and resumed Jev calls when global routing was off | High |
| Agent router returns `permissionDecision: "allow"` | Routing can auto-approve the rewritten agent launch instead of leaving approval to Claude's normal permission flow | High; actual effect depends on Claude version and permission rules |
| Jev's data-sharing scope is broader than the documentation implies | Delegated task text can include file contents or earlier conversation context; log redaction does not sanitize outbound prompts | High for sensitive workloads; Jev must already be opted into |
| Project configuration reader follows symlinks and can block on a FIFO | Reads outside the intended file; potential hook delay and an incomplete read-size boundary | Medium, adjacent hardening |
| Agent hook only accepts `Task`, not `Agent` | Agent evaluations, logs, and rewrites can be skipped on current tool payloads | Medium, compatibility issue |

This assessment establishes code paths and synthetic reproductions, not evidence of an attack
or actual secret disclosure. No real credentials or user prompts were used in the probes.

## 1. Project configuration has too much authority

Relevant code: [project loader and merge](../hooks/complexity_router.py), especially
`load_project_config`, `_OVERRIDE_KEY_TYPES`, and `merge_project_config`.

The override is loaded automatically from `<cwd>/.claude/model-switcher.json`. Its `routing`
and `complexity` sections are merged over user-owned settings. Threshold changes were an
intentional feature in [ADR-0003](adr/0003-routing-toggle-and-project-override.md), but parsing
untrusted JSON safely does not make its policy choices trustworthy.

The validation table is not a real allowlist: keys missing from `_OVERRIDE_KEY_TYPES` are still
copied. Consequently, projects can also set `routing.agents`, replace `routing.generic_agents`,
and change decision-notice visibility. A future routing setting could become project-controlled
without an explicit security decision.

### Reproduced effects

- A synthetic request scoring 2 stayed on the simple tier with global thresholds 3/7, but a
  project threshold of 1 selected the complex tier.
- Global `routing.enabled: false` plus global `jev.enabled: true` made zero API calls initially.
  Adding project `routing.enabled: true` resulted in one call to the mocked Jev transport.
- Project `routing.agents: true` overrode global `routing.agents: false` and produced a rewrite.
- Project `routing.generic_agents: ["Explore"]` changed a normally excluded specialist into
  an eligible agent and rewrote it to `heavy-task-fable`.
- An unknown synthetic routing key survived the merge.

The impact is constrained by globally configured model names and installed agents. These paths
do not prove arbitrary code execution or arbitrary destination selection. However, projects
can disable Jev and manipulate the offline fallback, so a successful Jev result is not a
defence against project-controlled routing policy.

### Required changes

1. Make the user's global routing and agent-routing opt-outs authoritative. Repository content
   must never turn either switch back on. Preserve the disabled state for malformed global
   settings too.
2. Replace open-ended section merging with an explicit field allowlist and type/range checks.
   Keep model names, generic-agent membership, network consent, logging consent, and future
   unknown fields under user control.
3. Make repository overrides opt-out-only by default: they may disable routing, agent routing,
   Jev, and content display. They may not increase authority or change thresholds/tier menus.
4. Preserve legitimate per-project tuning through user-owned project settings outside the
   checkout, keyed by a canonical project path, or an explicit user-owned trust mechanism.
   A repository must not be able to declare itself trusted. Do not treat `.gitignore` as a
   trust control. Start with exact path matching and document subdirectory/worktree behaviour.
5. Report the effective policy and its source in status/decision metadata so users can see
   which project overrides were applied or ignored, without logging prompt bodies.

**Compatibility:** users relying on "globally off, project on" or repository-supplied thresholds
will need migration instructions. This changes behaviour documented in README and ADR-0003;
it should not be presented as a transparent refactor. Claude's own workspace trust is useful
defence in depth, but it does not implement a field-level policy for this custom JSON file.

## 2. Model routing must not grant tool approval

Relevant code: [`agent_router.run`](../hooks/agent_router.py), the `hookSpecificOutput` assembled
after a successful rewrite. The probe reproduced `permissionDecision: "allow"`. An existing
test in [test_agent_router.py](../tests/test_agent_router.py) explicitly requires that value.

Anthropic documents that hook `allow` can skip a permission prompt. Its current permission
documentation also says matching deny/ask rules still apply. Therefore this is an unnecessary
approval decision, not evidence that all Claude security controls or child-tool permissions
are bypassed. [Hook reference](https://code.claude.com/docs/en/hooks#pretooluse-decision-control),
[permission rules](https://code.claude.com/docs/en/permissions#extend-permissions-with-hooks).

**Required fix:** omit `permissionDecision` and retain `hookEventName`, the complete
`updatedInput`, the routing explanation, and any user-facing notice. Do not replace `allow`
with unconditional `ask` or `defer`: those impose different permission behaviour. The official
SDK documentation explicitly supports input changes without a permission decision, leaving
normal permission evaluation in place.
[Input modification semantics](https://code.claude.com/docs/en/agent-sdk/hooks#modify-tool-input).

Verify this using a supported Claude version with allow, ask, and deny policies for the
rewritten target. Check that other hooks and child tools retain their own permission handling.
Some users may see legitimate permission prompts that were previously suppressed.

## 3. Make the Jev data boundary precise and controllable

Relevant code: [`agent_router.decide`](../hooks/agent_router.py),
[`evaluate_route`](../hooks/complexity_router.py), and `build_request`, `evaluate`, `_redact`,
`write_log`, and `record_result` in [jev_router.py](../hooks/jev_router.py).

### What is already protected

- Jev is disabled by default. Projects cannot directly enable Jev, content logging, or exchange
  display, replace model names, or select an API destination.
- The endpoint is fixed and HTTPS is used; redirects are refused.
- Credentials are loaded from the environment or a private credential file, not project JSON.
- Evaluation has a bounded deadline and response size, validated choices, and offline fallback.
- Full local content logging and session exchange display need separate user opt-ins.
- Local logs and notices redact the configured credential and some recognizable token forms.

These controls should remain. The synthetic checks confirmed the project opt-in restrictions
and credential redaction in local logs.

### Gaps and their impact

The prompt hook sends the user prompt. The agent hook sends **the full delegated task prompt**.
That text is generated by Claude and may contain source excerpts, command output, or context
from earlier turns. A probe with a synthetic file-context marker confirmed it reached the
outbound request unchanged. The integration does not independently walk repository files or
read transcripts for this call, but those contents can still arrive through the delegated text.
The current "not files or history" wording in README and SECURITY.md is therefore too broad.

Redaction runs when writing logs or preparing notices, after the request body is built.
A synthetic credential pasted into the prompt was present in the outbound body but redacted
in the corresponding local log. Logging protection is not outbound data protection.

`shadow` mode still transmits content. Turning detailed logs/display off also does not stop
transmission. A timeout or later rejection of a Jev answer cannot retract a request already sent.
Global Jev opt-in currently applies wherever eligible hooks run, without a separate user-owned
project approval or separate consent for delegated context. A project opt-out also only applies
at the exact working directory; a session started below that directory does not inherit it.

### Required changes

1. Correct README and SECURITY.md to distinguish user prompts, delegated task text, outbound
   requests, local logs, and session notices. State that credentials are sent to the official
   endpoint for authentication; "credentials stay local" is too absolute.
2. Add an explicit, user-owned control for Jev evaluation of delegated prompts. Recommend
   disabling that path by default until the user opts into sharing the broader context.
   Offline agent routing can continue independently.
3. Provide user-owned per-project external-evaluation permissions, with clearly documented
   path scope and a visible effective state. Prefer explicit project consent for new setup;
   retaining an all-project mode must be a deliberate choice. Repository files cannot grant it.
4. Before transport, detect at least the configured credential and narrowly defined token
   patterns. For a match, retain offline routing and record a static reason such as
   `sensitive_content`; do not put the matched secret or rejected prompt into logs/notices.
   This is defence in depth, not a guarantee that arbitrary secrets or personal data are found.
5. Keep request origin, transmission state, fallback reason, and consent source visible in
   metadata. A denied external evaluation must not stop Claude from processing the user's task.
6. Explain log retention, backups/rotation, uninstall retention, and the possibility that
   terminal/session capture retains displayed content. Do not claim provider-side retention or
   training guarantees without verifying the provider's applicable terms separately.

New setting names and schema should be recorded in an ADR before implementation. The names
above describe proposed behaviour, not flags already available in v0.4.0. The explicit
`model-switcher jev` smoke command is a separate user-requested operation; document how the new
controls interact with it, and apply outbound sensitive-content checks there as well.

## 4. Related fixes found during the review

### Bound and validate project configuration reads

`load_project_config` checks `stat().st_size` and then calls `read_text()`. It follows symlinks,
does not reject non-regular files, and does not bound the actual read independently of the
earlier size check. A synthetic symlink was followed; a local FIFO stalled until the probe's
one-second subprocess deadline killed it. FIFOs are not Git-tracked objects, so that case
requires local creation; symlinks can be part of a checkout.

Open without following symlinks, validate the opened descriptor as a regular file, and read
at most the limit plus one byte. Handle a symlinked `.claude` directory and file replacement
races deliberately. Malformed or unsupported files should retain the user-owned policy without
blocking. Keep the implementation stdlib-only and cover supported operating systems.

### Support current agent tool payloads

[`merge_settings.py`](../scripts/merge_settings.py) registers a `Task` matcher, and
[`agent_router.run`](../hooks/agent_router.py) rejects any other tool name. The same synthetic
task rewritten under `Task` produced no output or evaluation under `Agent`.

Anthropic identifies `Agent` as the current name and documents the historical rename.
[Subagent documentation](https://code.claude.com/docs/en/sub-agents#restrict-which-subagents-can-be-spawned).
Settings aliases should not be assumed to normalize the actual hook payload.

Support both exact names and migrate the installer's owned matcher without disturbing unrelated
hooks or uninstall restoration. Implement the Jev delegated-context consent boundary before
expanding this coverage, otherwise a compatibility fix could unexpectedly increase data sharing.

## Implementation scope and rollout

| Work | Files likely affected | Verification |
|---|---|---|
| Authoritative global opt-outs, explicit project allowlist, safe project reads | `hooks/complexity_router.py`; project policy helper if needed | Both hook paths; hostile override matrix; bounded reads |
| Permission-neutral agent rewrites | `hooks/agent_router.py` | No permission decision emitted; complete input preserved; real Claude permission behaviour |
| Jev project/origin consent and outbound content checks | Both routers, `hooks/jev_router.py`, `config/config.example.json` | No transport for denied scope/content; offline fallback; metadata contains no rejected content |
| Effective policy reporting and user controls | `scripts/cli.py`, `scripts/status_report.py`, `scripts/routing_report.py` | Standalone installed CLI, atomic writes, clear source and reason |
| Agent/Task compatibility and upgrade | `scripts/merge_settings.py`, `install.sh` if a new module is introduced | Both tool names; preservation of unrelated hooks; lifecycle install/uninstall |
| Public contract and migration | `README.md`, `SECURITY.md`, new ADR, references to ADR-0003/0015/0016/0017 | Examples match actual defaults; obsolete promises are removed |

Prioritize the global-disable guarantee and removal of auto-approval as a focused security
patch, together with corrected privacy wording and targeted regressions. The larger user-owned
project-policy and Jev consent design needs migration documentation because it narrows behaviour
that v0.4.0 deliberately supported. Keep that change reviewable separately if necessary.
Do not silently modify an existing user's Jev/logging configuration or delete their logs as part
of the assessment. No release, commit, push, or installation was performed for this review.

## Verification completed and release acceptance checks

Completed against the unchanged v0.4.0 runtime:

```sh
.venv/bin/python -m pytest tests/test_complexity_router.py tests/test_agent_router.py tests/test_jev_router.py -q
# 396 passed
```

Additional isolated probes reproduced all behaviours described above. They used temporary
directories, a synthetic credential, a mocked credential loader and transport, and an external
deadline for the FIFO probe. No live Claude permission-bypass test or live Jev call was made.
Passing existing tests does not resolve these concerns: tests currently expect project
re-enablement and explicit hook `allow`.

Required regression cases for the implementation:

- Global routing/agent opt-outs survive every project override, with no API calls or routing
  logs when routing is off. Invalid global configuration remains off.
- Unknown project fields are ignored; thresholds/tier changes require user-owned permission;
  specialist membership cannot be changed by repository content; opt-outs still work.
- Symlink, directory, FIFO, oversized/growing, malformed, and deeply nested project files are
  handled without unbounded reads or blocking.
- Both Task and Agent paths preserve unchanged inputs and never emit permission decisions.
  Confirm actual rewrites and permission prompts/denials in a temporary Claude setup.
- Unapproved projects and delegated contexts never reach Jev transport, including shadow mode.
  Sensitive-content fallback never leaks rejected content into logs or notices.
- Existing timeout, malformed-response, low-confidence, credential-redaction, log-rotation,
  and offline-only tests continue to pass.
- Run the full suite and per-file line/branch coverage gate, plus temporary-install lifecycle
  tests and the CI Python/OS matrix before releasing fixes.
