#!/usr/bin/env bash
# Capture real output from a throwaway install, for tools/make_demo_gif.py to replay.
#
# The README GIFs are a replay of genuine output, not a mock-up. This runs the real
# installer and CLI against a sandbox CLAUDE_DIR, then rewrites sandbox paths to the
# ones a user would actually see.
#
# Privacy: `learn` prints the terms it derived from local transcripts, which come from
# whatever the operator happens to work on. The aggregate accuracy table is kept; the
# term lists are dropped, so nothing from a private codebase ends up in a public asset.
#
# Usage: tools/capture_demo.sh [output-dir]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$REPO_DIR/.demo-capture}"
rm -rf "$OUT"; mkdir -p "$OUT"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
SANDBOX="$TMP/claude"
mkdir -p "$SANDBOX/model-switcher"

# A three-tier config, so the demo shows the routing ladder rather than the bare default.
cat > "$SANDBOX/model-switcher/config.json" <<'JSON'
{"models": {"complex": "fable", "standard": "sonnet", "simple": "haiku"},
 "complexity": {"threshold": 5, "standard_threshold": 3},
 "routing": {"enabled": true}, "statusline": {"wrap_command": null}}
JSON

CLAUDE_DIR="$SANDBOX" "$REPO_DIR/install.sh" --skip-model > "$OUT/install.txt" 2>&1

export MODEL_SWITCHER_HOME="$SANDBOX/model-switcher"
MS="$SANDBOX/model-switcher/model-switcher"

# Listing first: it should show what the installer put there, not later working files.
(
  cd "$SANDBOX/model-switcher"
  for entry in *; do
    if [ "$entry" != "__pycache__" ]; then printf '%s ' "$entry"; fi
  done
) | fold -s -w 96 > "$OUT/listing.txt"
printf '\n' >> "$OUT/listing.txt"

# Fill in the rate table, exactly as a user would straight after installing.
"$MS" pricing --offline --yes > "$OUT/pricing.txt" 2>&1

"$MS" explain "refactor the auth module and migrate the schema" > "$OUT/explain1.txt" 2>&1
"$MS" explain "what does this function do?" > "$OUT/explain2.txt" 2>&1

"$MS" learn > "$OUT/learn_full.txt" 2>&1 || true
# Keep the aggregates; drop every learned-term line (see the privacy note above).
# awk rather than `sed '/x/,+1d'`, which is a GNU extension BSD sed silently mishandles.
awk '/^strongest signals/ {skip = 2} skip > 0 {skip--; next} {print}' "$OUT/learn_full.txt" \
  > "$OUT/learn_trimmed.txt"
if grep -q "^strongest signals" "$OUT/learn_trimmed.txt"; then
  echo "refusing to continue: learned terms survived the privacy filter" >&2
  exit 1
fi

# A statusline over a transcript shaped like a mixed session: cheap model for most of the
# work, one delegation to the heavy one.
python3 - "$SANDBOX" > "$OUT/statusline.txt" <<'PY'
import json, os, pathlib, subprocess, sys
sandbox = pathlib.Path(sys.argv[1])
usage = {"input_tokens": 2000, "output_tokens": 3000, "cache_read_input_tokens": 300000,
         "cache_creation_input_tokens": 40000,
         "cache_creation": {"ephemeral_1h_input_tokens": 40000, "ephemeral_5m_input_tokens": 0}}
rows = [("claude-haiku-4-5", usage)] * 6 + [("claude-sonnet-5", usage)] * 2 + [("claude-fable-5", usage)]
lines = [json.dumps({"type": "user", "message": {}})]
lines += [json.dumps({"type": "assistant", "message": {"id": f"m{i}", "model": m, "usage": u}})
          for i, (m, u) in enumerate(rows)]
transcript = sandbox / "session.jsonl"
transcript.write_text("\n".join(lines) + "\n")
payload = json.dumps({"model": {"display_name": "Haiku 4.5"}, "transcript_path": str(transcript)})
out = subprocess.run(
    [sys.executable, str(sandbox / "model-switcher" / "cost_statusline.py")],
    input=payload, capture_output=True, text=True,
    env={**os.environ, "MODEL_SWITCHER_HOME": str(sandbox / "model-switcher")},
)
if not out.stdout.strip():
    sys.exit(f"statusline produced nothing: {out.stderr.strip()}")
print(out.stdout.strip())
PY

# Make the sandbox look like a normal machine.
sed -i '' "s|$SANDBOX|~/.claude|g; s|/private~|~|g; s|$REPO_DIR|~/model-switcher|g; s|$HOME|~|g" \
  "$OUT"/*.txt

echo "captured to $OUT:"
wc -l "$OUT"/*.txt
