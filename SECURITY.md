# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/jig21nesh/model-switcher/security/advisories/new)
rather than opening a public issue. Include what you observed, the steps to reproduce it, and the
impact you think it has. You can expect an initial response within 7 days.

## What this project touches

The local scorer and statusline run entirely on your machine. Optional `jev.enabled: true`
sends the current prompt (not transcripts, files, or session history) and configured model names
to `https://api.typesafe.ai/v1/systemone`. Jev is disabled on fresh installs. Projects can disable
it but cannot enable it or turn on content logging. `model-switcher jev` explicitly sends one
evaluation; `--offline` only previews it. The pricing refresh command also uses HTTPS.

| Surface | What it is | Trust |
|---|---|---|
| Prompt text | Passed to the `UserPromptSubmit` hook on every prompt | Untrusted |
| `~/.claude/model-switcher/config.json` | Your models, thresholds and pricing table | Untrusted input, user-owned |
| `<project>/.claude/model-switcher.json` | Per-project routing override | Untrusted, size-capped, fails open |
| Session transcript (`.jsonl`) | Read by the statusline to price tokens | Untrusted |
| `~/.claude/settings.json`, `~/.claude/CLAUDE.md` | Modified by the installer | Backed up once, marker-managed, reversible |

## Design guarantees

- **Fails open, never closed against the user.** A hook failure exits 0 with no output so your
  prompt still goes through; it never blocks or erases what you typed.
- **Untrusted data is never executed.** Prompt text, transcript content and config values are
  parsed with the stdlib JSON parser and treated as data — never `eval`'d, never interpolated into
  a shell command, never used unvalidated to build a filesystem path.
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
- **Credentials stay local.** Jev reads `TYPESAFE_API_KEY` or a private `jev-api-key` file in the
  install directory, never a key embedded in config. HTTP redirects are refused. A worker process
  bounds DNS, TLS and response reads; failures fall back to local routing, with no retries.
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
