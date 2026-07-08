# ADR-0002: Imperative directive + managed CLAUDE.md block for delegation compliance

Status: accepted (2026-07-08)

## Context

ADR-0001 established that routing is advisory: hooks cannot switch a request's model, so complex
prompts are routed by injecting a delegation directive into context. A live test proved the
weakness: on a prompt scored 6/10 (COMPLEX), the directive was verifiably injected into the
session (confirmed in the transcript JSONL) but the session model performed the task itself with
zero subagent calls and no acknowledgement. Two causes: per-turn injected context is weighted far
below system-prompt content, and the directive's escape hatch ("if clearly trivial, handle it
in-session") granted the model discretion.

Anthropic's own model switching (claude.ai routing, the API `fallbacks` parameter, Claude Code's
`opusplan`) happens in the server or harness — layers unavailable to hook-based extensions. Any
fix must therefore raise instruction compliance rather than enforce a switch.

## Decision

- Reword the injected directive as MANDATORY ROUTING POLICY: first-action framing ("your FIRST
  action must be spawning 'heavy-task'"), a policy rationale (low-cost session tier), and a
  narrow escape hatch (only when the user explicitly declines delegation).
- Ship a routing-policy block in the repo (`config/claude-md-section.md`) and have the installer
  manage it inside the user's global `~/.claude/CLAUDE.md` between `<!-- model-switcher:begin/end -->`
  markers, via `scripts/manage_claude_md.py`:
  - no CLAUDE.md → create it containing only the block; record `created_claude_md` in the manifest;
  - existing CLAUDE.md → one-time backup (`CLAUDE.md.model-switcher.bak`), block appended after the
    user's content, never modifying it;
  - re-install → replace only the text between markers (idempotent, upgrades ship new policy text);
  - uninstall → remove the block only; delete the file only when the installer created it and
    nothing else was added.
- Skip scoring for `<agent-message>` peer relays (added to the meta-prompt guard).

## Consequences

- Compliance rises from "sometimes" to near-universal: system-prompt-level policy plus an
  imperative per-turn directive is the strongest signal available to an extension. It remains
  probabilistic — a hard per-prompt model switch is impossible on this platform.
- Borderline prompts (score = threshold) now delegate more consistently, paying heavy-model rates
  plus subagent startup; users tune `complexity.threshold` if too eager.
- The installer now touches three user files (settings.json, CLAUDE.md, agents/) — all
  marker/manifest-managed, backed up once, and fully reverted by `install.sh --uninstall`.
- The statusline doubles as the compliance audit trail: a COMPLEX turn billed only at cheap-model
  rates indicates a skipped delegation.
