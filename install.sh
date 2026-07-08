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

UNINSTALL=0
SKIP_MODEL=0
for arg in "$@"; do
  case "$arg" in
    --uninstall) UNINSTALL=1 ;;
    --skip-model) SKIP_MODEL=1 ;;
    *) echo "usage: install.sh [--uninstall] [--skip-model]" >&2; exit 2 ;;
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
  rm -f "$INSTALL_DIR/complexity_router.py" "$INSTALL_DIR/cost_statusline.py" \
    "$INSTALL_DIR/merge_settings.py" "$MANIFEST" "$AGENTS_DIR/heavy-task.md"
  rm -rf "$INSTALL_DIR/state"
  echo "model-switcher removed. Kept: $CONFIG. Restart Claude Code sessions to apply."
  exit 0
fi

mkdir -p "$INSTALL_DIR/state" "$AGENTS_DIR"
cp "$REPO_DIR/hooks/complexity_router.py" "$REPO_DIR/statusline/cost_statusline.py" \
  "$REPO_DIR/scripts/merge_settings.py" "$INSTALL_DIR/"
[ -f "$CONFIG" ] || cp "$REPO_DIR/config/config.example.json" "$CONFIG"

COMPLEX_MODEL="$(read_config_model complex opus)"
SIMPLE_MODEL="$(read_config_model simple sonnet)"

python3 - "$REPO_DIR/agents/heavy-task.md" "$AGENTS_DIR/heavy-task.md" "$COMPLEX_MODEL" <<'PY'
import re, sys
from pathlib import Path
source, target, model = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
target.write_text(re.sub(r"^model: .*$", f"model: {model}", source.read_text(), count=1, flags=re.MULTILINE))
PY

SET_MODEL_ARGS=()
[ "$SKIP_MODEL" -eq 0 ] && SET_MODEL_ARGS=(--set-model "$SIMPLE_MODEL")

python3 "$INSTALL_DIR/merge_settings.py" install \
  --settings "$SETTINGS" --install-dir "$INSTALL_DIR" --config "$CONFIG" --manifest "$MANIFEST" \
  "${SET_MODEL_ARGS[@]}"

echo "model-switcher installed:"
echo "  hook:       UserPromptSubmit -> $INSTALL_DIR/complexity_router.py"
echo "  statusline: $INSTALL_DIR/cost_statusline.py"
echo "  agent:      $AGENTS_DIR/heavy-task.md (model: $COMPLEX_MODEL)"
[ "$SKIP_MODEL" -eq 0 ] && echo "  session model set to: $SIMPLE_MODEL (previous value saved in $MANIFEST)"
echo "  config:     $CONFIG  <- set your pricing here (rates: https://claude.com/pricing)"
echo "Restart Claude Code sessions to apply."
