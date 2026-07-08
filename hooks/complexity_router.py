"""UserPromptSubmit hook: score prompt complexity and direct delegation to the heavy-task agent."""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path

logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="model-switcher %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PRICING_URL = "https://claude.com/pricing"
DEFAULT_THRESHOLD = 5
STATE_MAX_AGE_SECONDS = 7 * 24 * 3600
SESSION_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")

STRONG_KEYWORDS = (
    "refactor", "architect", "redesign", "implement", "migrate", "migration",
    "rewrite", "overhaul", "scaffold", "from scratch", "build a", "build an",
    "create a", "create an", "set up", "debug", "root cause", "investigate",
    "integrate", "deploy", "end-to-end", "e2e", "audit", "threat model",
    "optimize", "optimise", "design a", "design an",
)
MODERATE_KEYWORDS = (
    "test", "tests", "database", "schema", "api", "endpoint", "security",
    "performance", "config", "configure", "pipeline", "terraform", "docker",
    "kubernetes", "multiple", "across", "entire", "whole",
)
CONNECTIVES = (" then ", "after that", "and also", "as well as", "finally")
NUMBERED_STEP_RE = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
FILE_PATH_RE = re.compile(r"\S+\.(?:py|ts|tsx|js|jsx|tf|sql|go|rs|java|json|ya?ml|md|sh)\b")


def home_dir() -> Path:
    return Path(os.environ.get("MODEL_SWITCHER_HOME", str(Path.home() / ".claude" / "model-switcher")))


def load_config() -> dict:
    try:
        config = json.loads((home_dir() / "config.json").read_text(encoding="utf-8"))
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {}


def _has_keyword(keyword: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def score_prompt(prompt: str) -> int:
    text = prompt.lower()
    words = len(text.split())
    strong = [k for k in STRONG_KEYWORDS if _has_keyword(k, text)]
    moderate = [k for k in MODERATE_KEYWORDS if _has_keyword(k, text)]

    score = 0
    if strong:
        score += 4 + min(len(strong) - 1, 2)
    score += min(len(moderate), 3)
    if words >= 150:
        score += 2
    elif words >= 50:
        score += 1
    if len(NUMBERED_STEP_RE.findall(prompt)) >= 2:
        score += 2
    if sum(text.count(c) for c in CONNECTIVES) >= 2:
        score += 1
    if "```" in prompt:
        score += 1
    if len(FILE_PATH_RE.findall(text)) >= 2:
        score += 1
    # A short pure question with no strong task verb is a lookup, not a task.
    if words < 25 and prompt.rstrip().endswith("?") and not strong:
        score = min(score, 2)
    return max(0, min(score, 10))


def models_configured(config: dict) -> bool:
    models = config.get("models")
    if not isinstance(models, dict):
        return False
    return bool(models.get("complex")) and bool(models.get("simple"))


def pricing_configured(config: dict) -> bool:
    pricing = config.get("pricing_usd_per_mtok")
    if not isinstance(pricing, dict):
        return False
    for rates in pricing.values():
        if isinstance(rates, dict) and rates and all(isinstance(v, (int, float)) for v in rates.values()):
            return True
    return False


def _state_path(session_id: str) -> Path | None:
    if not SESSION_ID_RE.fullmatch(session_id or ""):
        return None
    return home_dir() / "state" / f"{session_id}.json"


def _load_state(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path | None, state: dict) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _cleanup_stale_state(path.parent)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        logger.warning("cannot persist session state: %s", exc)


def _cleanup_stale_state(state_dir: Path) -> None:
    cutoff = time.time() - STATE_MAX_AGE_SECONDS
    for f in state_dir.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


def build_context(prompt: str, session_id: str, config: dict) -> str:
    parts: list[str] = []
    state_path = _state_path(session_id)
    state = _load_state(state_path)
    state_dirty = False

    if not models_configured(config):
        if not state.get("models_nagged"):
            parts.append(
                "[model-switcher] Model routing is not configured. Ask the user to confirm which Claude "
                "models to use for 'complex' tasks (suggest: opus) and 'simple' tasks (suggest: sonnet). "
                f"Once confirmed, write their choices into the 'models' section of {home_dir() / 'config.json'} "
                "and then continue with the user's request."
            )
            state["models_nagged"] = True
            state_dirty = True
    else:
        threshold = config.get("complexity", {}).get("threshold", DEFAULT_THRESHOLD)
        if not isinstance(threshold, int):
            threshold = DEFAULT_THRESHOLD
        score = score_prompt(prompt)
        if score >= threshold:
            complex_model = config["models"]["complex"]
            parts.append(
                f"[model-switcher] Complexity score {score}/10 (threshold {threshold}): this prompt is "
                f"classified COMPLEX. Delegate it to the 'heavy-task' subagent (model: {complex_model}) via "
                "the Agent tool, passing the full request and any context it needs, then relay the subagent's "
                "result to the user. If this prompt is clearly a trivial follow-up despite its score, handle "
                "it in-session and briefly say why."
            )

    if not pricing_configured(config) and not state.get("pricing_nagged"):
        parts.append(
            "[model-switcher] Offline cost calculation is not configured: the pricing table in "
            f"{home_dir() / 'config.json'} has no rates. Ask the user to fill in 'pricing_usd_per_mtok' "
            "($ per million tokens: input, output, cache_write, cache_read for each model) and point them to "
            f"the current rates at {PRICING_URL}"
        )
        state["pricing_nagged"] = True
        state_dirty = True

    if state_dirty:
        _save_state(state_path, state)
    return "\n\n".join(parts)


def run(stdin_text: str) -> str:
    try:
        payload = json.loads(stdin_text)
    except ValueError:
        logger.warning("invalid hook input, passing prompt through")
        return ""
    if not isinstance(payload, dict):
        return ""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return ""
    context = build_context(prompt, str(payload.get("session_id", "")), load_config())
    if not context:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}})


def main() -> int:
    # Routing must never block the user's prompt: any failure exits 0 with no output.
    try:
        output = run(sys.stdin.read())
        if output:
            print(output)
    except Exception as exc:  # noqa: BLE001
        logger.warning("router failed, passing prompt through: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
