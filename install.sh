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
                (models.complex/simple, complexity.threshold, pricing_usd_per_mtok)
Pricing rates:  https://claude.com/pricing
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
    "$INSTALL_DIR/claude-md-section.md" "$MANIFEST"
  rm -rf "$INSTALL_DIR/state"
  echo "model-switcher removed. Kept: $CONFIG. Restart Claude Code sessions to apply."
  exit 0
fi

mkdir -p "$INSTALL_DIR/state" "$AGENTS_DIR"
cp "$REPO_DIR/hooks/complexity_router.py" "$REPO_DIR/statusline/cost_statusline.py" \
  "$REPO_DIR/scripts/merge_settings.py" "$REPO_DIR/scripts/manage_claude_md.py" \
  "$REPO_DIR/config/claude-md-section.md" "$INSTALL_DIR/"
[ -f "$CONFIG" ] || cp "$REPO_DIR/config/config.example.json" "$CONFIG"

COMPLEX_MODEL="$(read_config_model complex fable)"
SIMPLE_MODEL="$(read_config_model simple sonnet)"

AGENT_INFO=$(python3 "$REPO_DIR/scripts/generate_agent.py" install \
  --source "$REPO_DIR/agents/heavy-task.md" --agents-dir "$AGENTS_DIR" \
  --model "$COMPLEX_MODEL" --manifest "$MANIFEST")

SET_MODEL_ARGS=()
if [ "$SKIP_MODEL" -eq 0 ]; then SET_MODEL_ARGS=(--set-model "$SIMPLE_MODEL"); fi

python3 "$INSTALL_DIR/merge_settings.py" install \
  --settings "$SETTINGS" --install-dir "$INSTALL_DIR" --config "$CONFIG" --manifest "$MANIFEST" \
  "${SET_MODEL_ARGS[@]}"
python3 "$INSTALL_DIR/manage_claude_md.py" install \
  --claude-md "$CLAUDE_DIR/CLAUDE.md" --block-file "$INSTALL_DIR/claude-md-section.md" \
  --manifest "$MANIFEST"

echo "model-switcher installed:"
echo "  hook:       UserPromptSubmit -> $INSTALL_DIR/complexity_router.py"
echo "  statusline: $INSTALL_DIR/cost_statusline.py"
echo "  ${AGENT_INFO}"
echo "  policy:     managed block in $CLAUDE_DIR/CLAUDE.md"
if [ -f "$CLAUDE_DIR/CLAUDE.md.model-switcher.bak" ]; then
  echo "              (pre-install backup: $CLAUDE_DIR/CLAUDE.md.model-switcher.bak)"
fi
if [ "$SKIP_MODEL" -eq 0 ]; then echo "  session model set to: $SIMPLE_MODEL (previous value saved in $MANIFEST)"; fi
echo "  config:     $CONFIG  <- set your pricing here (rates: https://claude.com/pricing)"
echo "Restart Claude Code sessions to apply."
