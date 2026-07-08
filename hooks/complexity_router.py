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
# Scoring beyond this many characters adds no signal and regex work on huge pastes must stay off
# the interactive path; truncation itself is treated as a length signal.
SCORE_MAX_CHARS = 10_000
STATE_MAX_AGE_SECONDS = 7 * 24 * 3600
SESSION_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9._\[\]-]{1,64}")

STRONG_KEYWORDS = (
    "refactor", "architect", "architecture", "redesign", "implement", "migrate", "migration",
    "rewrite", "overhaul", "scaffold", "debug", "investigate", "integrate", "audit",
    "optimize", "optimise", "review", "analyze", "analyse", "diagnose", "troubleshoot",
    "profile", "regression", "harden", "vulnerability", "vulnerabilities", "deadlock",
    "crash", "multi-tenancy", "from scratch", "build a", "build an", "design a", "design an",
    "root cause", "end-to-end", "e2e", "threat model", "race condition", "memory leak",
    "sql injection", "figure out",
)
MODERATE_KEYWORDS = (
    "test", "database", "schema", "api", "endpoint", "security", "performance", "config",
    "configure", "pipeline", "terraform", "docker", "kubernetes", "multiple", "across",
    "entire", "whole", "everywhere", "codebase", "backend", "frontend", "fix", "bug",
    "error", "broken", "broke", "failing", "slow", "slower", "latency", "leak", "patch",
    "exploit", "xss", "csrf", "oauth", "sso", "saml", "websocket", "retry", "backoff",
    "concurrency", "thread", "deploy", "deployment", "rollout", "production", "rename",
    "set up", "create a", "create an", "add support",
)
CONNECTIVES = (" then ", "after that", "and also", "as well as", "finally")
NUMBERED_STEP_RE = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
# Anchored per-token check: an unanchored \S+ scan is quadratic on long unbroken tokens.
EXT_RE = re.compile(r".+\.(?:py|ts|tsx|js|jsx|tf|sql|go|rs|java|json|ya?ml|md|sh)$")
TRACEBACK_RE = re.compile(
    r"traceback \(most recent call last\)|^\s*at .+:\d+|\b[a-z_]*(?:error|exception)\b\s*[:(]",
    re.MULTILINE,
)
DEFINITIONAL_RE = re.compile(
    r"^(?:what(?:'s| is| are| does| do)\b|explain what\b|explain the difference\b"
    r"|describe what\b|tell me about\b)|\bdifference between\b"
)
AFFIRMATION_RE = re.compile(
    r"^(?:yes|yep|yeah|ok(?:ay)?|sure|sounds good|go ahead|continue|proceed|do it|approved|lgtm)\b"
)
NEGATION_TAIL_RE = re.compile(
    r"(?:\bdon'?t|\bdo not|\bdoesn'?t|\bwon'?t|\bnever|\bwithout|\bavoid|\bno need to|\binstead of)"
    r"\s+(?:\w+\s+){0,2}$"
)
COMMAND_TAG_RE = re.compile(
    r"</?(?:command-name|command-message|command-args|local-command-stdout|local-command-caveat"
    r"|agent-message)(?:\s[^>]*)?>"
)


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    if " " in keyword:
        return re.compile(rf"\b{re.escape(keyword)}\b")
    # Match common inflections: refactoring, migrated, crashes, debugging.
    stem = keyword[:-1] if keyword.endswith("e") else keyword
    return re.compile(
        rf"\b(?:{re.escape(keyword)}(?:s|es|d|ed)?|{re.escape(stem)}ing|{re.escape(keyword)}{keyword[-1]}ing)\b"
    )


STRONG_PATTERNS = tuple((k, _keyword_pattern(k)) for k in STRONG_KEYWORDS)
MODERATE_PATTERNS = tuple((k, _keyword_pattern(k)) for k in MODERATE_KEYWORDS)


def home_dir() -> Path:
    return Path(os.environ.get("MODEL_SWITCHER_HOME", str(Path.home() / ".claude" / "model-switcher")))


def load_config() -> dict:
    try:
        config = json.loads((home_dir() / "config.json").read_text(encoding="utf-8"))
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {}


def _strong_hits(text: str) -> list[str]:
    hits = []
    for keyword, pattern in STRONG_PATTERNS:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 48):match.start()]
            if not NEGATION_TAIL_RE.search(window):
                hits.append(keyword)
                break
    return hits


def score_prompt(prompt: str) -> int:
    truncated = len(prompt) > SCORE_MAX_CHARS
    text = prompt[:SCORE_MAX_CHARS].lower()
    tokens = text.split()
    words = len(tokens)
    strong = _strong_hits(text)
    moderate = [k for k, p in MODERATE_PATTERNS if p.search(text)]

    score = 0
    if strong:
        score += 5 + min(len(strong) - 1, 2)
    score += min(len(moderate), 3)
    if truncated or words >= 150:
        score += 2
    elif words >= 50:
        score += 1
    if len(NUMBERED_STEP_RE.findall(text)) >= 2:
        score += 2
    if sum(text.count(c) for c in CONNECTIVES) >= 2:
        score += 1
    if "```" in text:
        score += 1
    if sum(1 for t in tokens if EXT_RE.match(t.rstrip(".,;:!?)\"'"))) >= 2:
        score += 1
    if TRACEBACK_RE.search(text):
        score += 3

    # Short pure questions without a task verb are lookups; definitional questions are lookups
    # even when they mention task vocabulary; short affirmations continue in-session work.
    if words < 25 and text.rstrip().endswith("?") and not strong:
        score = min(score, 2)
    if words < 25 and DEFINITIONAL_RE.search(text):
        score = min(score, 2)
    if words <= 12 and AFFIRMATION_RE.match(text):
        score = min(score, 2)
    return max(0, min(score, 10))


def threshold_from(config: dict) -> float:
    complexity = config.get("complexity")
    if not isinstance(complexity, dict):
        complexity = {}
    value = complexity.get("threshold", DEFAULT_THRESHOLD)
    # bool is a subclass of int; "threshold": true must not become threshold 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("invalid complexity.threshold %r, using default %s", value, DEFAULT_THRESHOLD)
        return float(DEFAULT_THRESHOLD)
    clamped = max(1.0, min(float(value), 10.0))
    if clamped != float(value):
        logger.warning("complexity.threshold %r clamped to %s", value, clamped)
    return clamped


def _heavy_agent_name(model: str) -> str:
    # Keep in sync with scripts/generate_agent.py: the installer stamps this name into the agent
    # file so the model is visible in Claude Code's task line (e.g. heavy-task-opus).
    suffix = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return f"heavy-task-{suffix}" if suffix else "heavy-task"


def models_configured(config: dict) -> bool:
    models = config.get("models")
    if not isinstance(models, dict):
        return False
    # Names are interpolated into Claude's context: only plausible model identifiers qualify.
    return all(
        isinstance(models.get(key), str) and MODEL_NAME_RE.fullmatch(models[key])
        for key in ("complex", "simple")
    )


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
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("cannot persist session state: %s", exc)


def _cleanup_stale_state(state_dir: Path) -> None:
    # Only ever delete files this tool created: session-id-named, non-symlink, in a real dir.
    if state_dir.is_symlink():
        return
    cutoff = time.time() - STATE_MAX_AGE_SECONDS
    for f in state_dir.glob("*.json"):
        if f.is_symlink() or not SESSION_ID_RE.fullmatch(f.stem):
            continue
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
        threshold = threshold_from(config)
        score = score_prompt(prompt)
        if score >= threshold:
            complex_model = config["models"]["complex"]
            agent = _heavy_agent_name(complex_model)
            parts.append(
                f"[model-switcher] MANDATORY ROUTING POLICY — complexity score {score}/10 (threshold "
                f"{threshold:g}): this prompt is classified COMPLEX. This session runs on the low-cost "
                f"model tier; complex work must be executed by the '{agent}' subagent (configured "
                f"model: {complex_model}). Do not perform this task yourself: your FIRST action must be "
                f"spawning '{agent}' via your subagent tool (named Agent or Task depending on version), "
                "passing the user's full request and any context it needs. Relay the subagent's result to "
                "the user afterwards. Answer directly only if the user's message explicitly says not to "
                "delegate."
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
    # Inside a subagent, injecting a delegation directive would recurse heavy-task into itself.
    if payload.get("agent_id"):
        return ""
    # Slash commands, skill invocations, and local-command echoes are meta-prompts: routing them
    # is meaningless and would waste the once-per-session nags on a command turn.
    if prompt.lstrip().startswith("/") or COMMAND_TAG_RE.search(prompt):
        return ""
    context = build_context(prompt, str(payload.get("session_id", "")), load_config())
    if not context:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}})


def main() -> int:
    # Routing must never block the user's prompt: any failure exits 0 with no output.
    # Invariants: stdout on exit 0 is injected into Claude's context (never print debug here),
    # and exit 2 would erase the user's prompt (never exit non-zero).
    try:
        output = run(sys.stdin.read())
        if output:
            print(output)
    except Exception as exc:  # noqa: BLE001
        logger.warning("router failed, passing prompt through: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
