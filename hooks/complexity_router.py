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
# Only consulted when a third tier is configured; the middle band is [standard, complex).
DEFAULT_STANDARD_THRESHOLD = 3
# Agent filename prefixes, kept in sync with scripts/generate_agent.py. The top tier keeps its
# original prefix so existing installs, manifests and CLAUDE.md policy blocks stay valid.
TIER_PREFIXES = {"complex": "heavy-task", "standard": "mid-task"}
TIER_LABELS = {"complex": "COMPLEX", "standard": "MODERATE"}
# A project override is a small settings file; anything bigger is not one.
PROJECT_CONFIG_MAX_BYTES = 64 * 1024
# Scoring beyond this many characters adds no signal and regex work on huge pastes must stay off
# the interactive path; truncation itself is treated as a length signal.
SCORE_MAX_CHARS = 10_000
STATE_MAX_AGE_SECONDS = 7 * 24 * 3600
SESSION_ID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9._\[\]-]{1,64}")

# Learned weights produced by scripts/analyze_history.py. The format, and the tokenizer below,
# are specified in docs/classifier-schema.md — keep the two in step (tests assert they agree).
CLASSIFIER_SCHEMA_VERSION = 1
CLASSIFIER_MAX_BYTES = 512 * 1024
CLASSIFIER_MAX_TERMS = 1000
CLASSIFIER_TERM_RE = re.compile(r"^[a-z][a-z0-9-]{2,23}$")
CLASSIFIER_SPLIT_RE = re.compile(r"[^a-z0-9-]+")
# Hard ceiling on how far learned weights may move a score, whatever the artifact claims. The
# hand-tuned signals keep authority: a skewed or hostile classifier cannot dominate them.
CLASSIFIER_MAX_ADJUSTMENT = 3.0
# What a prompt scores once a lookup cap applies, however much else it matched.
CAPPED_SCORE = 2

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


def load_project_config(cwd: object) -> dict:
    if not isinstance(cwd, str) or not cwd:
        return {}
    path = Path(cwd) / ".claude" / "model-switcher.json"
    try:
        if path.stat().st_size > PROJECT_CONFIG_MAX_BYTES:
            logger.warning("project override too large, ignoring: %s", path)
            return {}
        override = json.loads(path.read_text(encoding="utf-8"))
        return override if isinstance(override, dict) else {}
    except (OSError, ValueError):
        return {}


# An override value that fails its type check is dropped so the global value stays in effect —
# ADR-0003: override failures fall open to the global config, never to hardcoded defaults.
def _is_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


_OVERRIDE_KEY_TYPES = {
    ("routing", "enabled"): lambda v: isinstance(v, bool),
    # A project may drop to two tiers or opt into three, but never name a model: the agent files
    # are generated from the global config at install time (ADR-0003).
    ("routing", "tiers"): lambda v: v == "auto" or (not isinstance(v, bool) and v in (2, 3)),
    ("complexity", "threshold"): _is_number,
    ("complexity", "standard_threshold"): _is_number,
}


def merge_project_config(config: dict, override: dict) -> dict:
    # Per-project overrides cover behavioural knobs only; models and pricing stay global because
    # the heavy-task agent is generated from the global config at install time.
    merged = dict(config)
    for section in ("routing", "complexity"):
        extra = override.get(section)
        if not isinstance(extra, dict):
            continue
        base = merged.get(section)
        combined = dict(base) if isinstance(base, dict) else {}
        for key, value in extra.items():
            check = _OVERRIDE_KEY_TYPES.get((section, key))
            if check is not None and not check(value):
                logger.warning("invalid %s.%s in project override, keeping global value", section, key)
                continue
            combined[key] = value
        merged[section] = combined
    return merged


def routing_enabled(config: dict) -> bool:
    routing = config.get("routing")
    if not isinstance(routing, dict):
        return True
    value = routing.get("enabled", True)
    if isinstance(value, bool):
        return value
    logger.warning("invalid routing.enabled %r, routing stays enabled", value)
    return True


def _strong_hits(text: str) -> list[str]:
    hits = []
    for keyword, pattern in STRONG_PATTERNS:
        for match in pattern.finditer(text):
            window = text[max(0, match.start() - 48):match.start()]
            if not NEGATION_TAIL_RE.search(window):
                hits.append(keyword)
                break
    return hits


def classifier_terms(text: str) -> set[str]:
    """Tokenize exactly as docs/classifier-schema.md specifies, so artifacts are portable."""
    tokens = CLASSIFIER_SPLIT_RE.split(text[:SCORE_MAX_CHARS].lower())
    return {t for t in (token.strip("-") for token in tokens) if CLASSIFIER_TERM_RE.match(t)}


def learned_adjustment(text: str, classifier: dict) -> tuple[float, dict[str, float]]:
    """Bounded contribution from learned weights, plus the terms that produced it."""
    weights = classifier.get("terms") or {}
    if not weights:
        return 0.0, {}
    matched = {term: weights[term] for term in classifier_terms(text) if term in weights}
    limit = classifier.get("max_adjustment", CLASSIFIER_MAX_ADJUSTMENT)
    return max(-limit, min(limit, sum(matched.values()))), matched


def final_score(base: float, adjustment: float, caps: list) -> int:
    """The routed score from its parts.

    Separate from analyse_prompt so the offline sensitivity report can ask what a prompt would
    have scored without one of its contributions without re-deriving the rounding, the clamp or
    the lookup cap — a counterfactual that disagreed with routing would be worse than none.
    """
    score = base + adjustment
    if caps:
        score = min(score, CAPPED_SCORE)
    return max(0, min(int(round(score)), 10))


def analyse_prompt(prompt: str, classifier: dict | None = None) -> dict:
    """Score a prompt and record why, so `explain` and routing can never disagree."""
    truncated = len(prompt) > SCORE_MAX_CHARS
    text = prompt[:SCORE_MAX_CHARS].lower()
    tokens = text.split()
    words = len(tokens)
    strong = _strong_hits(text)
    moderate = [k for k, p in MODERATE_PATTERNS if p.search(text)]

    signals: list[tuple[str, int]] = []
    if strong:
        signals.append((f"task verbs ({', '.join(strong[:3])})", 5 + min(len(strong) - 1, 2)))
    if moderate:
        signals.append((f"domain terms ({', '.join(moderate[:3])})", min(len(moderate), 3)))
    if truncated or words >= 150:
        signals.append(("long prompt", 2))
    elif words >= 50:
        signals.append(("medium prompt", 1))
    if len(NUMBERED_STEP_RE.findall(text)) >= 2:
        signals.append(("numbered steps", 2))
    if sum(text.count(c) for c in CONNECTIVES) >= 2:
        signals.append(("chained requests", 1))
    if "```" in text:
        signals.append(("code block", 1))
    if sum(1 for t in tokens if EXT_RE.match(t.rstrip(".,;:!?)\"'"))) >= 2:
        signals.append(("multiple file paths", 1))
    if TRACEBACK_RE.search(text):
        signals.append(("stack trace", 3))

    base = sum(points for _, points in signals)
    adjustment, matched = learned_adjustment(text, classifier) if classifier else (0.0, {})

    # Short pure questions without a task verb are lookups; definitional questions are lookups
    # even when they mention task vocabulary; short affirmations continue in-session work.
    # These run after the learned adjustment so that learned weights cannot talk a lookup
    # into being delegated.
    caps = []
    if words < 25 and text.rstrip().endswith("?") and not strong:
        caps.append("short question")
    if words < 25 and DEFINITIONAL_RE.search(text):
        caps.append("definitional question")
    if words <= 12 and AFFIRMATION_RE.match(text):
        caps.append("affirmation")

    return {
        "signals": signals,
        "base": base,
        "learned": round(adjustment, 2),
        "matched_terms": matched,
        "caps": caps,
        "score": final_score(base, adjustment, caps),
    }


def score_prompt(prompt: str, classifier: dict | None = None) -> int:
    return analyse_prompt(prompt, classifier)["score"]


def load_classifier() -> dict:
    """Read learned weights, or return nothing at all. Never raises, never blocks a prompt."""
    path = home_dir() / "classifier.json"
    try:
        if path.stat().st_size > CLASSIFIER_MAX_BYTES:
            logger.warning("classifier is larger than %s bytes, ignoring it", CLASSIFIER_MAX_BYTES)
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != CLASSIFIER_SCHEMA_VERSION:
        return {}
    scoring = data.get("scoring")
    if not isinstance(scoring, dict) or not isinstance(scoring.get("terms"), dict):
        return {}

    weights = {}
    for term, weight in scoring["terms"].items():
        if len(weights) >= CLASSIFIER_MAX_TERMS:
            break
        # Terms came from prompt text: they are matched as plain strings, never compiled.
        if isinstance(term, str) and CLASSIFIER_TERM_RE.match(term) and _is_number(weight):
            weights[term] = float(weight)

    limit = scoring.get("max_adjustment", CLASSIFIER_MAX_ADJUSTMENT)
    if not _is_number(limit) or not 0 < limit <= CLASSIFIER_MAX_ADJUSTMENT:
        limit = CLASSIFIER_MAX_ADJUSTMENT
    return {"terms": weights, "max_adjustment": float(limit)} if weights else {}


def _numeric_setting(complexity: dict, key: str, default: int) -> float:
    value = complexity.get(key, default)
    # bool is a subclass of int; "threshold": true must not become threshold 1.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        logger.warning("invalid complexity.%s %r, using default %s", key, value, default)
        return float(default)
    clamped = max(1.0, min(float(value), 10.0))
    if clamped != float(value):
        logger.warning("complexity.%s %r clamped to %s", key, value, clamped)
    return clamped


def _complexity_section(config: dict) -> dict:
    complexity = config.get("complexity")
    return complexity if isinstance(complexity, dict) else {}


def threshold_from(config: dict) -> float:
    return _numeric_setting(_complexity_section(config), "threshold", DEFAULT_THRESHOLD)


def standard_threshold_from(config: dict) -> float:
    """Lower edge of the middle band. Always strictly below the complex threshold."""
    complexity = _complexity_section(config)
    threshold = threshold_from(config)
    value = _numeric_setting(complexity, "standard_threshold", DEFAULT_STANDARD_THRESHOLD)
    if value >= threshold:
        # Overlapping bands would make the middle tier unreachable and the config a lie.
        adjusted = max(1.0, threshold - 1.0)
        logger.warning("complexity.standard_threshold %g is not below threshold %g, using %g",
                       value, threshold, adjusted)
        return adjusted
    return value


def agent_name_for(model: str, tier: str = "complex") -> str:
    # Keep in sync with scripts/generate_agent.py: the installer stamps this name into the agent
    # file so the model is visible in Claude Code's task line (e.g. heavy-task-fable).
    prefix = TIER_PREFIXES.get(tier, TIER_PREFIXES["complex"])
    suffix = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return f"{prefix}-{suffix}" if suffix else prefix


def _valid_model(models: object, key: str) -> str | None:
    # Names are interpolated into Claude's context: only plausible model identifiers qualify.
    if not isinstance(models, dict):
        return None
    value = models.get(key)
    return value if isinstance(value, str) and MODEL_NAME_RE.fullmatch(value) else None


def models_configured(config: dict) -> bool:
    models = config.get("models")
    return all(_valid_model(models, key) is not None for key in ("complex", "simple"))


def tiers_configured(config: dict) -> int:
    """How many model tiers are in play: 3 when a valid middle model exists and is not disabled."""
    has_standard = _valid_model(config.get("models"), "standard") is not None
    routing = config.get("routing")
    requested = routing.get("tiers", "auto") if isinstance(routing, dict) else "auto"
    if requested == "auto" or isinstance(requested, bool) or requested not in (2, 3):
        if requested != "auto":
            logger.warning("invalid routing.tiers %r, falling back to auto", requested)
        return 3 if has_standard else 2
    if requested == 3 and not has_standard:
        logger.warning("routing.tiers is 3 but models.standard is missing or invalid, using 2 tiers")
        return 2
    return int(requested)


def select_tier(score: float, config: dict) -> str | None:
    """Which tier a score routes to, or None to answer in-session."""
    if score >= threshold_from(config):
        return "complex"
    if tiers_configured(config) == 3 and score >= standard_threshold_from(config):
        return "standard"
    return None


def routing_ladder(config: dict) -> list[dict]:
    """The score bands a config produces, cheapest first, with the model that serves each.

    One description of the ladder, shared by `explain` and the installer, so what a user is
    shown at install time cannot drift from what the router will actually do. Bands are
    half-open and written out in full: a lone "threshold 5" says nothing about where a middle
    tier begins, which is why a three-tier config was hard to verify by eye.
    """
    models = config.get("models")
    threshold = threshold_from(config)
    rungs: list[dict] = []
    if tiers_configured(config) == 3:
        standard = standard_threshold_from(config)
        rungs.append({"tier": None, "band": f"score < {standard:g}"})
        rungs.append({"tier": "standard", "band": f"{standard:g} <= score < {threshold:g}"})
    else:
        rungs.append({"tier": None, "band": f"score < {threshold:g}"})
    rungs.append({"tier": "complex", "band": f"score >= {threshold:g}"})

    for rung in rungs:
        tier = rung["tier"]
        model = _valid_model(models, tier or "simple")
        rung["label"] = TIER_LABELS.get(tier, "SIMPLE").lower()
        rung["model"] = model or "(unset)"
        rung["destination"] = agent_name_for(model, tier) if tier and model else "answered in-session"
    return rungs


# Severity for health_warnings(). Only BROKEN interrupts a session.
BROKEN = "broken"
ADVICE = "advice"


def claude_dir() -> Path:
    return home_dir().parent


def resolve_alias(alias: object, pricing: dict) -> str | None:
    """Best pricing key for a configured model name ('fable' -> 'claude-fable-5').

    Config names models the way a user types them, including context-window suffixes like
    'opus[1m]'; the pricing table is keyed by id. Shortest match wins, so 'opus' lands on the
    current base id rather than an older dated variant.
    """
    if not isinstance(alias, str) or not alias.strip() or not isinstance(pricing, dict):
        return None
    needle = re.sub(r"\[.*?\]", "", alias).strip().lower()
    if not needle:
        return None
    if alias in pricing:
        return alias
    matches = [key for key in pricing if needle in key.lower()]
    return min(sorted(matches), key=len) if matches else None


def _output_rate(key: str | None, pricing: dict) -> float | None:
    rates = pricing.get(key) if key else None
    value = rates.get("output") if isinstance(rates, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def health_warnings(config: dict, session_model: object = None, agents_dir: Path | None = None) -> list[tuple]:
    """Configuration problems detectable without reading a transcript, as (severity, message).

    Severity is the whole point of the split. BROKEN means routing cannot do what the config
    says — delegation has no agent to reach, or a tier is unreachable — and is worth
    interrupting a session for. ADVICE means the config works exactly as written and you may
    not want what it does; that is a report to read, not a notice to receive every session.
    Nagging about a deliberate choice on every new session is how a warning becomes wallpaper.

    Deliberately cheap — config, one model name and one directory listing — because the hook
    surfaces the BROKEN ones in-session, where touching the transcript corpus would be far too
    slow to run on a prompt. The costly checks live in the `status` command instead.
    """
    warnings: list[tuple] = []
    models = config.get("models")
    pricing = config.get("pricing_usd_per_mtok")
    pricing = pricing if isinstance(pricing, dict) else {}
    complex_model = _valid_model(models, "complex")
    simple_model = _valid_model(models, "simple")
    session = session_model if isinstance(session_model, str) and session_model.strip() else simple_model

    complex_rate = _output_rate(resolve_alias(complex_model, pricing), pricing)
    session_rate = _output_rate(resolve_alias(session, pricing), pricing)
    # A heavy tier is meant to be dearer than the session model — that is the whole shape of the
    # tool. What is worth reporting is the opposite: delegating to something no more expensive
    # than what you are already running, where the heavy tier is not a step up at all.
    if complex_rate is not None and session_rate is not None and complex_rate <= session_rate:
        warnings.append((ADVICE,
            f"models.complex '{complex_model}' (${complex_rate:g}/Mtok out) costs no more than your "
            f"session model '{session}' (${session_rate:g}) — delegation moves work to a model that "
            "is not a step up, so the heavy tier is not buying you anything"))

    if session_model and simple_model and resolve_alias(session_model, pricing) != resolve_alias(
        simple_model, pricing
    ):
        warnings.append((ADVICE,
            f"your session runs '{session_model}' but models.simple is '{simple_model}' — the cheap "
            "tier is configured but not actually in use"))

    for key in ("complex", "standard", "simple"):
        model = _valid_model(models, key)
        if model and pricing and resolve_alias(model, pricing) is None:
            warnings.append((ADVICE,
                f"models.{key} '{model}' has no entry in pricing_usd_per_mtok, so its cost is invisible"))

    # Only meaningful against a real agents directory. Its absence means this is not an install,
    # not that an agent went missing, and reporting that would be noise in every sandbox.
    if agents_dir is not None and agents_dir.is_dir():
        for tier in ("complex", "standard"):
            model = _valid_model(models, tier)
            if not model or (tier == "standard" and tiers_configured(config) != 3):
                continue
            expected = f"{agent_name_for(model, tier)}.md"
            if not (agents_dir / expected).is_file():
                warnings.append((BROKEN,
                    f"no agent file '{expected}' — {tier} prompts are told to delegate to an agent "
                    "that does not exist. Re-run install.sh."))

    complexity = _complexity_section(config)
    raw_standard = _numeric_setting(complexity, "standard_threshold", DEFAULT_STANDARD_THRESHOLD)
    if tiers_configured(config) == 3 and raw_standard >= threshold_from(config):
        warnings.append((BROKEN,
            f"complexity.standard_threshold ({raw_standard:g}) is not below threshold "
            f"({threshold_from(config):g}); the middle tier is unreachable as written"))
    return warnings


def in_session_checks_enabled(config: dict) -> bool:
    checks = config.get("checks")
    enabled = checks.get("in_session", True) if isinstance(checks, dict) else True
    return enabled if isinstance(enabled, bool) else True


def session_model_from_settings() -> str | None:
    try:
        settings = json.loads((claude_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    model = settings.get("model") if isinstance(settings, dict) else None
    return model if isinstance(model, str) and MODEL_NAME_RE.fullmatch(model) else None


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


def delegation_directive(score: int, tier: str, config: dict) -> str:
    model = config["models"][tier]
    agent = agent_name_for(model, tier)
    edge = threshold_from(config) if tier == "complex" else standard_threshold_from(config)
    return (
        f"[model-switcher] MANDATORY ROUTING POLICY — complexity score {score}/10 (threshold "
        f"{edge:g}): this prompt is classified {TIER_LABELS[tier]}. This session runs on the low-cost "
        f"model tier; work at this level must be executed by the '{agent}' subagent (configured "
        f"model: {model}). Do not perform this task yourself: your FIRST action must be "
        f"spawning '{agent}' via your subagent tool (named Agent or Task depending on version), "
        "passing the user's full request and any context it needs. Relay the subagent's result to "
        "the user afterwards. Answer directly only if the user's message explicitly says not to "
        "delegate."
    )


def build_context(prompt: str, session_id: str, config: dict) -> str:
    parts: list[str] = []
    state_path = _state_path(session_id)
    state = _load_state(state_path)
    state_dirty = False

    if not models_configured(config):
        if not state.get("models_nagged"):
            parts.append(
                "[model-switcher] Model routing is not configured. Ask the user to confirm which Claude "
                "models to use for 'complex' tasks (suggest: fable) and 'simple' tasks (suggest: sonnet). "
                f"Once confirmed, write their choices into the 'models' section of {home_dir() / 'config.json'} "
                "and then continue with the user's request."
            )
            state["models_nagged"] = True
            state_dirty = True
    else:
        score = score_prompt(prompt, load_classifier())
        tier = select_tier(score, config)
        if tier is not None:
            parts.append(delegation_directive(score, tier, config))

    # Guarded on the session flag first: when the notice has already fired, none of the checks
    # run at all, so the cost of this is paid once per session rather than once per prompt.
    if not state.get("health_nagged") and in_session_checks_enabled(config):
        broken = [
            message for severity, message
            in health_warnings(config, session_model_from_settings(), claude_dir() / "agents")
            if severity == BROKEN
        ]
        if broken:
            listed = "\n".join(f"- {message}" for message in broken)
            parts.append(
                "[model-switcher] This install is misconfigured and routing cannot work as "
                "written:\n" + listed +
                "\nTell the user about this once, briefly, then carry on with their request. "
                "'model-switcher status' shows the full picture; "
                '"checks": {"in_session": false} in config.json silences this.'
            )
        state["health_nagged"] = True
        state_dirty = True

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
    config = merge_project_config(load_config(), load_project_config(payload.get("cwd")))
    if not routing_enabled(config):
        return ""
    context = build_context(prompt, str(payload.get("session_id", "")), config)
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
