#!/usr/bin/env bash
# Install or remove model-switcher for all local Claude Code sessions (CLI + VS Code).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_DIR="${CLAUDE_DIR:-$HOME/.claude}"
INSTALL_DIR="$CLAUDE_DIR/model-switcher"
AGENTS_DIR="$CLAUDE_DIR/agents"
SETTINGS="$CLAUDE_DIR/settings.json"
CONFIG="$INSTALL_DIR/config.json"
MANIFEST="$INSTALL_DIR/installed.json"

usage() {
  cat <<EOF
model-switcher installer — per-prompt model routing and offline cost statusline for Claude Code.

Usage: ./install.sh [OPTIONS]

Installs into $CLAUDE_DIR (override with the CLAUDE_DIR environment variable):
  - UserPromptSubmit hook that scores every prompt and routes complex ones
    to the 'heavy-task' subagent (your configured heavy model)
  - cost statusline that prices each turn/session offline from the transcript
    (wraps your existing statusline if you have one)
  - heavy-task subagent definition (model taken from config.json)
  - marker-managed routing-policy block in CLAUDE.md (one-time backup kept)
  - merged entries in settings.json (one-time backup kept; never overwrites)

Options:
  --skip-model    Leave the session model in settings.json untouched.
                  Default: set it to models.simple from config.json (the
                  previous value is recorded and restored on uninstall).
  --uninstall     Remove everything the installer added: hook, statusline,
                  agent, CLAUDE.md block, and settings entries; restores your
                  previous model and statusline from the manifest.
                  Kept: $CLAUDE_DIR/model-switcher/config.json (models, pricing).
  -h, --help      Show this help and exit.

Configuration:  $CLAUDE_DIR/model-switcher/config.json
                (models.complex/simple, routing.enabled, complexity.threshold, pricing_usd_per_mtok)
Pricing rates:  ships pre-filled; check or refresh with ./bin/model-switcher pricing
                (source of truth: config/pricing.json in this repo)
Documentation:  https://github.com/jig21nesh/model-switcher

Restart Claude Code sessions after installing or uninstalling.
EOF
}

UNINSTALL=0
SKIP_MODEL=0
for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=1 ;;
    --skip-model) SKIP_MODEL=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "install.sh: unknown option '$arg'" >&2
      echo "Try './install.sh --help' for more information." >&2
      exit 2
      ;;
  esac
done

read_config_model() {
  python3 - "$CONFIG" "$1" "$2" <<'PY'
import json, sys
from pathlib import Path
path, key, default = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
try:
    value = json.loads(path.read_text()).get("models", {}).get(key)
except (OSError, ValueError):
    value = None
print(value or default)
PY
}

if [ "$UNINSTALL" -eq 1 ]; then
  python3 "$REPO_DIR/scripts/merge_settings.py" uninstall \
    --settings "$SETTINGS" --install-dir "$INSTALL_DIR" --config "$CONFIG" --manifest "$MANIFEST"
  python3 "$REPO_DIR/scripts/manage_claude_md.py" uninstall \
    --claude-md "$CLAUDE_DIR/CLAUDE.md" --manifest "$MANIFEST"
  python3 "$REPO_DIR/scripts/generate_agent.py" uninstall \
    --agents-dir "$AGENTS_DIR" --manifest "$MANIFEST"
  rm -f "$INSTALL_DIR/complexity_router.py" "$INSTALL_DIR/cost_statusline.py" \
    "$INSTALL_DIR/merge_settings.py" "$INSTALL_DIR/manage_claude_md.py" \
    "$INSTALL_DIR/claude-md-section.md" "$INSTALL_DIR/cli.py" \
    "$INSTALL_DIR/analyze_history.py" "$INSTALL_DIR/update_pricing.py" \
    "$INSTALL_DIR/pricing.json" "$INSTALL_DIR/model-switcher" "$MANIFEST"
  # Stale bytecode from the removed scripts would otherwise shadow a later install.
  rm -rf "$INSTALL_DIR/state" "$INSTALL_DIR/__pycache__"
  echo "model-switcher removed. Kept: $CONFIG. Restart Claude Code sessions to apply."
  exit 0
fi

mkdir -p "$INSTALL_DIR/state" "$AGENTS_DIR"
cp "$REPO_DIR/hooks/complexity_router.py" "$REPO_DIR/statusline/cost_statusline.py" \
  "$REPO_DIR/scripts/merge_settings.py" "$REPO_DIR/scripts/manage_claude_md.py" \
  "$REPO_DIR/config/claude-md-section.md" "$INSTALL_DIR/"
# The maintenance CLI and everything it needs, so pricing/learn/explain keep working after the
# clone is deleted. Copied flat; bin/model-switcher handles both layouts.
cp "$REPO_DIR/scripts/cli.py" "$REPO_DIR/scripts/analyze_history.py" \
  "$REPO_DIR/scripts/update_pricing.py" "$REPO_DIR/config/pricing.json" "$INSTALL_DIR/"
cp "$REPO_DIR/bin/model-switcher" "$INSTALL_DIR/model-switcher"
chmod +x "$INSTALL_DIR/model-switcher"
[ -f "$CONFIG" ] || cp "$REPO_DIR/config/config.example.json" "$CONFIG"

COMPLEX_MODEL="$(read_config_model complex fable)"
SIMPLE_MODEL="$(read_config_model simple sonnet)"
# Optional middle tier. Empty means a two-tier install, which is the default.
STANDARD_MODEL="$(read_config_model standard "")"

AGENT_INFO=$(python3 "$REPO_DIR/scripts/generate_agent.py" install \
  --source "$REPO_DIR/agents/heavy-task.md" --agents-dir "$AGENTS_DIR" \
  --model "$COMPLEX_MODEL" --manifest "$MANIFEST" --tier complex)

if [ -n "$STANDARD_MODEL" ]; then
  STANDARD_AGENT_INFO=$(python3 "$REPO_DIR/scripts/generate_agent.py" install \
    --source "$REPO_DIR/agents/mid-task.md" --agents-dir "$AGENTS_DIR" \
    --model "$STANDARD_MODEL" --manifest "$MANIFEST" --tier standard)
else
  # models.standard was removed since the last install; drop the agent it left behind.
  python3 "$REPO_DIR/scripts/generate_agent.py" uninstall \
    --agents-dir "$AGENTS_DIR" --manifest "$MANIFEST" --tier standard >/dev/null
  STANDARD_AGENT_INFO=""
fi

SET_MODEL_ARGS=()
if [ "$SKIP_MODEL" -eq 0 ]; then SET_MODEL_ARGS=(--set-model "$SIMPLE_MODEL"); fi

python3 "$INSTALL_DIR/merge_settings.py" install \
  --settings "$SETTINGS" --install-dir "$INSTALL_DIR" --config "$CONFIG" --manifest "$MANIFEST" \
  ${SET_MODEL_ARGS[@]+"${SET_MODEL_ARGS[@]}"}
python3 "$INSTALL_DIR/manage_claude_md.py" install \
  --claude-md "$CLAUDE_DIR/CLAUDE.md" --block-file "$INSTALL_DIR/claude-md-section.md" \
  --manifest "$MANIFEST"

echo "model-switcher installed:"
echo "  hook:       UserPromptSubmit -> $INSTALL_DIR/complexity_router.py"
echo "  statusline: $INSTALL_DIR/cost_statusline.py"
echo "  ${AGENT_INFO}"
if [ -n "$STANDARD_AGENT_INFO" ]; then
  echo "  ${STANDARD_AGENT_INFO}"
  echo "  routing:    3 tiers — simple in-session, moderate -> mid-task, complex -> heavy-task"
else
  echo "  routing:    2 tiers — simple in-session, complex -> heavy-task"
  echo "              (set models.standard in config.json and re-run for a middle tier)"
fi
echo "  policy:     managed block in $CLAUDE_DIR/CLAUDE.md"
if [ -f "$CLAUDE_DIR/CLAUDE.md.model-switcher.bak" ]; then
  echo "              (pre-install backup: $CLAUDE_DIR/CLAUDE.md.model-switcher.bak)"
fi
if [ "$SKIP_MODEL" -eq 0 ]; then echo "  session model set to: $SIMPLE_MODEL (previous value saved in $MANIFEST)"; fi
echo "  config:     $CONFIG"
echo "              (pricing ships pre-filled; refresh it with ./bin/model-switcher pricing)"
echo "  cli:        $INSTALL_DIR/model-switcher (works without this repo)"
echo "Next:"
echo "  $INSTALL_DIR/model-switcher explain \"<a prompt>\"   how a prompt scores and where it routes"
echo "  $INSTALL_DIR/model-switcher learn                  tune routing from your own history"
echo "  $INSTALL_DIR/model-switcher pricing                check your token rates"
echo "  (add $INSTALL_DIR to PATH, or symlink the CLI, to type just 'model-switcher')"
echo "Restart Claude Code sessions to apply."
