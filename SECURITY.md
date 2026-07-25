# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/jig21nesh/model-switcher/security/advisories/new)
rather than opening a public issue. Include what you observed, the steps to reproduce it, and the
impact you think it has. You can expect an initial response within 7 days.

## What this project touches

`model-switcher` runs entirely on your machine. It makes no network calls from the hook or the
statusline, and it sends nothing anywhere.

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
- **Nothing is logged.** Prompt content and pricing values are never written to logs or to disk by
  the hook. Errors are a single line on stderr.
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
