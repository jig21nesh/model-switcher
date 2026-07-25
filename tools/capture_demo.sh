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

# The in-session scene: what the router does on ordinary prompts, with no command run at all.
# Every score and every directive below is produced by driving the real hook and the real
# explain path; only the "> prompt" framing and the indent labels are added here.
python3 - "$SANDBOX" > "$OUT/session.txt" <<'PY'
import json, os, pathlib, re, subprocess, sys

sandbox = pathlib.Path(sys.argv[1])
home = sandbox / "model-switcher"
env = {**os.environ, "MODEL_SWITCHER_HOME": str(home)}

PROMPTS = [
    "what does this function do?",
    "fix the failing test in the config parser",
    "refactor the auth module, migrate the schema and add tests",
]


def hook(prompt):
    """The directive the hook injects, or None when it stays out of the way."""
    out = subprocess.run(
        [sys.executable, str(home / "complexity_router.py")],
        input=json.dumps({"prompt": prompt, "session_id": "demo"}),
        capture_output=True, text=True, env=env,
    ).stdout.strip()
    if not out:
        return None
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def score_and_verdict(prompt):
    out = subprocess.run(
        [sys.executable, str(home / "cli.py"), "explain", prompt],
        capture_output=True, text=True, env=env,
    ).stdout
    score = re.search(r"score (\d+)/10", out)
    return score.group(1) if score else "?"


for prompt in PROMPTS:
    print(f"> {prompt}")
    directive = hook(prompt)
    score = score_and_verdict(prompt)
    if directive is None:
        print(f"    score {score}/10 -> below the threshold, answered in-session on haiku")
        print()
        continue
    tier = re.search(r"classified (\w+)", directive)
    agent = re.search(r"'([\w-]+)' subagent", directive)
    print(f"    score {score}/10 -> {tier.group(1) if tier else '?'}")
    print("    injected into Claude's context, before Claude reads the prompt:")
    # Split at the colon and elide the rest: the real directive is far wider than the frame.
    preamble, _, classification = directive.split(". ")[0].partition("): ")
    print(f"      {preamble}):")
    print(f"      {classification}. ... must be executed by the '{agent.group(1)}' subagent")
    print(f"    Claude's first action: spawn {agent.group(1)}")
    print()
PY

if ! grep -q "heavy-task-" "$OUT/session.txt"; then
  echo "refusing to continue: the session capture shows no complex delegation" >&2
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
user = json.dumps({"type": "user", "message": {}})
assistant = [json.dumps({"type": "assistant", "message": {"id": f"m{i}", "model": m, "usage": u}})
             for i, (m, u) in enumerate(rows)]
# A second user turn near the end, so `turn` is the current exchange rather than the whole session.
lines = [user, *assistant[:-1], user, assistant[-1]]
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
