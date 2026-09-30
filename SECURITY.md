# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/jig21nesh/model-switcher/security/advisories/new)
rather than opening a public issue. Include what you observed, the steps to reproduce it, and the
impact you think it has. You can expect an initial response within 7 days.

## What this project touches

The local scorer and statusline run entirely on your machine. Optional Jev evaluation sends
the prompt, configured model choices, and routing question to `https://api.typesafe.ai/v1/systemone`.
For user prompts this includes anything pasted into the request. Separately enabled delegated
task evaluation sends Claude's task prompt, which may contain source excerpts, tool output, or
earlier conversation context. The evaluator does not independently read repository files or
transcripts; their contents can nevertheless be present in the text it receives.

Jev is disabled on fresh installs. Automatic calls require global `jev.enabled: true` and a
user-owned scope: default `scope: "projects"` requires the exact canonical working directory in
`allowed_projects`; `scope: "all"` explicitly permits every directory. `evaluate_agents: true`
is an additional opt-in for delegated task content. Shadow mode also sends data. Local display
and content-logging flags do not control transmission. Project files can opt out but cannot
grant consent. Project matching and repository opt-outs do not inherit into subdirectories.

`model-switcher jev` explicitly requests one evaluation independently of automatic scope;
`--offline` only previews it locally. Treat that preview as private input. The pricing refresh
command also uses HTTPS. No claim is made here about TypeSafe's server-side retention or
training policies; review the provider's applicable terms before sharing sensitive material.

| Surface | What it is | Trust |
|---|---|---|
| Prompt text | Passed to the `UserPromptSubmit` hook on every prompt | Untrusted |
| `~/.claude/model-switcher/config.json` | Your models, thresholds and pricing table | Untrusted input, user-owned |
| `project_settings` in the user-owned config | Exact-directory threshold/tier tuning | User-controlled; cannot enable globally disabled routing |
| `<project>/.claude/model-switcher.json` | Repository opt-outs only | Untrusted; allowlisted false values, bounded regular-file reads, no symlinks |
| Session transcript (`.jsonl`) | Read by the statusline to price tokens | Untrusted |
| `~/.claude/settings.json`, `~/.claude/CLAUDE.md` | Modified by the installer | Backed up once, marker-managed, reversible |

## Design guarantees

- **Fails open, never closed against the user.** A hook failure exits 0 with no output so your
  prompt still goes through; it never blocks or erases what you typed.
- **Untrusted data is never executed.** Prompt text, transcript content and config values are
  parsed with the stdlib JSON parser and treated as data — never `eval`'d, never interpolated into
  a shell command, never used unvalidated to build a filesystem path.
- **Repository settings cannot grant authority.** Global routing and agent-routing opt-outs
  remain off; malformed global settings also disable routing. Repository files can only
  disable explicitly listed features. Thresholds, tier menus, specialist membership, models,
  network consent and unknown settings cannot be supplied by repository content.
- **Routing never approves tools.** The Agent/Task hook changes input without returning a
  permission decision. Claude's permission evaluation still applies to the rewritten input.
- **Sensitive prompts fall back locally.** Before transport, the configured credential and
  recognizable bearer/token/private-key markers trigger `sensitive_content`. The rejected
  prompt, scoring terms and request/response bodies are omitted from logs and notices.
  This is a narrow detection guard, not comprehensive detection of secrets or personal data.
  Failed scope checks likewise log metadata only. A timeout cannot retract an attempted send.
- **Content logging is opt-in.** Eligible routing evaluations log offline and Jev decisions,
  or an explicit disabled status for Jev. Routing off produces no new evaluation logs.
  `jev.log_content: true`
  also records the prompt, matched scoring signals/terms, question instructions, request and response in owner-only rotating
  `logs/jev.jsonl` files. The authentication key and recognizable bearer credentials are redacted;
  arbitrary secrets in prose cannot be detected reliably. Treat these logs as private user data.
  No authorization headers or pricing values are logged. Errors are one line on stderr.
- **Session content display is opt-in.** `routing.show_decisions` shows only decision metadata
  by default. `routing.show_exchange: true` plus `jev.log_content: true` includes bounded,
  credential-redacted request/response previews in Claude's user-visible hook notice. Terminal
  controls are escaped, and these bodies are never added to the routing directive. Projects
  can hide this display but cannot enable it. Treat sessions displaying exchanges as private.
- **Credentials are restricted to authentication.** Jev reads `TYPESAFE_API_KEY` or a private
  `jev-api-key` file in the install directory, never a key embedded in config. It sends the key
  to the fixed official endpoint in the Authorization header. HTTP redirects are refused. A worker process
  bounds DNS, TLS and response reads; failures fall back to local routing, with no retries.
- **Retention is local and explicit.** Logs rotate into `jev.jsonl.1`; logs and credentials
  survive uninstall. Users can delete these files explicitly. Backups, terminal recordings and
  session capture can retain copies of content displayed in notices; hiding details does not
  erase earlier records. Ignore rules prevent ordinary Git additions but are not access controls.
- **Deletion is narrowly scoped.** The installer only ever removes files it created, identified by
  name and location, and skips symlinks.
- **Your setup is restorable.** `settings.json` and `CLAUDE.md` are backed up once before the first
  modification, and `./install.sh --uninstall` restores them from a manifest.

## Scope

In scope: anything that lets untrusted input (a prompt, a transcript, a project override file)
execute code, escape its intended path, exfiltrate data, or corrupt files outside the set the
installer manages.

Out of scope: the accuracy of the cost estimate (it is derived from transcript tokens and your own
pricing table — it is not your Anthropic bill), and the fact that delegation is advisory rather
than an enforced platform guarantee. Both are documented in `README.md`.
