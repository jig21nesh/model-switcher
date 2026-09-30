# Unreleased: routing security improvements

Draft notes for the next release, pending review and merge of
[PR #37](https://github.com/jig21nesh/model-switcher/pull/37).

## Changes

- Keep routing decisions under the user's control: repository settings can disable supported
  features, while threshold and tier changes belong in the user's own configuration.
- Keep model selection separate from tool approval: agent routing leaves permission decisions
  to Claude's normal checks and supports both current `Agent` and legacy `Task` tool names.
- Make Jev data sharing explicit: automatic evaluation requires an approved project scope,
  delegated-agent prompts require a separate opt-in, and recognized credential patterns cause
  offline fallback before sending. This guard does not detect every kind of sensitive data.

## Upgrading

Existing Jev configurations without a sharing scope fall back to offline routing. Choose
approved project paths or explicitly allow all projects, and enable delegated-agent evaluation
separately if needed. Move repository threshold/tier settings into the user-owned
`project_settings` map. Re-run the installer and restart Claude to load the updated hooks.
See [ADR-0018](adr/0018-routing-trust-boundaries.md#consequences-and-migration) for migration details.

## Acknowledgments

Thanks to **[Pooja Kiran Bharadwaj (@poojakira)](https://github.com/poojakira)** for highlighting
security concerns around project-level routing settings, tool approval handling, and sharing
prompt content with Jev. Her feedback prompted these security improvements.
