"""Statusline command: deterministic offline token-cost calculation from the session transcript."""

import json
import logging
import math
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="model-switcher %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

PRICING_URL = "https://claude.com/pricing"
WRAP_TIMEOUT_SECONDS = 3
RATE_KEYS = ("input", "output", "cache_write", "cache_read")
# Optional per-model extras. When absent, pricing falls back to the pre-existing
# behaviour, so configs written before these keys existed keep working unchanged.
# cache_write is the 5-minute-TTL rate (1.25x input); 1-hour writes cost 2x input.
CACHE_1H_KEY = "cache_write_1h"
FAST_KEY = "fast"
# A rate above this is a typo or a poisoned config, not a price.
MAX_RATE_USD_PER_MTOK = 10_000.0

# Segments are rendered in this order. "saved" and "routing" stay silent when they have nothing
# worth saying, so the default line stays short until something is actually notable.
DEFAULT_SEGMENTS = ("turn", "session", "saved", "tokens", "routing")
# Below half a cent, a savings figure is noise.
SAVINGS_MIN_USD = 0.005
# Savings are only measurable once a session has spanned more than one model. With a single
# model in the transcript nothing was ever routed, so any figure would be a hypothetical.
MIN_MODELS_FOR_SAVINGS = 2


def home_dir() -> Path:
    return Path(os.environ.get("MODEL_SWITCHER_HOME", str(Path.home() / ".claude" / "model-switcher")))


def load_config() -> dict:
    try:
        config = json.loads((home_dir() / "config.json").read_text(encoding="utf-8"))
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {}


def _is_rate(value: object) -> bool:
    # bool is a subclass of int; "input": true must not silently price tokens at $1/MTok.
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= MAX_RATE_USD_PER_MTOK
    )


def _usable_rates(rates: object) -> dict | None:
    """A rate block is usable only when all four required rates are sane numbers."""
    if not isinstance(rates, dict) or not all(_is_rate(rates.get(key)) for key in RATE_KEYS):
        return None
    usable = {key: float(rates[key]) for key in RATE_KEYS}
    if _is_rate(rates.get(CACHE_1H_KEY)):
        usable[CACHE_1H_KEY] = float(rates[CACHE_1H_KEY])
    return usable


def usable_pricing(config: dict) -> dict[str, dict]:
    pricing = config.get("pricing_usd_per_mtok")
    if not isinstance(pricing, dict):
        return {}
    usable = {}
    for model, rates in pricing.items():
        base = _usable_rates(rates)
        if base is None:
            continue
        fast = _usable_rates(rates.get(FAST_KEY))
        if fast is not None:
            base[FAST_KEY] = fast
        usable[model] = base
    return usable


def resolve_pricing(model_id: str, pricing: dict[str, dict]) -> tuple[str, dict] | None:
    """Resolve a transcript model id to the (pricing key, rates) that price it.

    The key matters as well as the rates: two ids that resolve to the same entry are the
    same model for the purpose of deciding whether a session actually spanned tiers.
    """
    if model_id in pricing:
        return model_id, pricing[model_id]
    # Dated releases (claude-sonnet-5-20250929) match their base entry (claude-sonnet-5).
    prefixes = [key for key in pricing if model_id.startswith(key)]
    if prefixes:
        key = max(prefixes, key=len)
        return key, pricing[key]
    return None


def match_pricing(model_id: str, pricing: dict[str, dict]) -> dict | None:
    resolved = resolve_pricing(model_id, pricing)
    return resolved[1] if resolved is not None else None


def parse_transcript(path: Path) -> tuple[list[dict], int, str]:
    """Deduped assistant usage, the line of the last real user message, and its timestamp.

    The timestamp is what lets subagent work be attributed to the current turn: it lives in a
    different file, so there is no line number to compare against.
    """
    entries: dict[str, dict] = {}
    last_user_line = -1
    last_user_ts = ""
    with path.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "user" and not obj.get("isMeta") and not obj.get("isSidechain"):
                last_user_line = i
                stamp = obj.get("timestamp")
                last_user_ts = stamp if isinstance(stamp, str) else ""
            elif obj.get("type") == "assistant":
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                # Streaming rewrites the same message id; the last entry carries final usage.
                msg_id = message.get("id") or obj.get("uuid") or f"line-{i}"
                stamp = obj.get("timestamp")
                entries[msg_id] = {
                    "model": str(message.get("model") or ""), "usage": usage, "line": i,
                    "timestamp": stamp if isinstance(stamp, str) else "",
                }
    return list(entries.values()), last_user_line, last_user_ts


def subagent_transcripts(transcript: Path) -> list[Path]:
    """This session's subagent transcripts, which Claude Code writes outside the session file.

    Layout is <project>/<session-id>.jsonl for the session and <project>/<session-id>/**/*.jsonl
    for every agent it spawned (plain subagents under subagents/, workflow agents under wf_*/).
    Naming the directory after the session id makes the attribution exact rather than a guess.
    Missing directory simply means no agent ran.
    """
    root = transcript.with_suffix("")
    try:
        if not root.is_dir():
            return []
        return sorted(path for path in root.rglob("*.jsonl") if path.is_file())
    except OSError:
        return []


def collect_entries(transcript: Path) -> tuple[list[dict], int]:
    """Every assistant entry that this session paid for, session file and subagents alike.

    Subagent entries carry no line number in the session file, so their place in the current
    turn is decided by timestamp against the last user message. Where a transcript has no
    timestamps at all, they count towards the session but never towards the turn — understating
    the turn is better than attributing an hour of agent work to whatever was typed last.
    """
    entries, last_user_line, last_user_ts = parse_transcript(transcript)
    for path in subagent_transcripts(transcript):
        try:
            spawned, _, _ = parse_transcript(path)
        except OSError as exc:
            logger.warning("cannot read subagent transcript: %s", exc)
            continue
        for entry in spawned:
            in_turn = bool(last_user_ts) and entry["timestamp"] >= last_user_ts
            # Sorts after last_user_line when in the turn, before it otherwise.
            entry["line"] = last_user_line + 1 if in_turn else -1
            entries.append(entry)
    return entries, last_user_line


def cache_write_tokens(usage: dict) -> tuple[int, int, int]:
    """Split cache-creation tokens into (5-minute, 1-hour, unsplit) buckets.

    When the per-TTL breakdown is present it is authoritative. A small number of entries
    carry a cache_creation_input_tokens total that disagrees with the breakdown it sits
    beside, and mixing the two there would double-count or go negative.
    """
    breakdown = usage.get("cache_creation")
    if isinstance(breakdown, dict):
        five_minute = _tokens(breakdown, "ephemeral_5m_input_tokens")
        one_hour = _tokens(breakdown, "ephemeral_1h_input_tokens")
        if five_minute or one_hour:
            return five_minute, one_hour, 0
    return 0, 0, _tokens(usage, "cache_creation_input_tokens")


def effective_rates(usage: dict, rates: dict) -> dict:
    """Fast mode runs the same model at premium rates, so it needs its own rate block."""
    if usage.get("speed") == "fast":
        fast = rates.get(FAST_KEY)
        if isinstance(fast, dict):
            return fast
    return rates


def usage_cost(usage: dict, rates: dict) -> float:
    five_minute, one_hour, unsplit = cache_write_tokens(usage)
    # A config with no 1-hour rate prices 1-hour writes exactly as it did before.
    one_hour_rate = rates.get(CACHE_1H_KEY, rates["cache_write"])
    return (
        _tokens(usage, "input_tokens") * rates["input"]
        + (five_minute + unsplit) * rates["cache_write"]
        + one_hour * one_hour_rate
        + _tokens(usage, "cache_read_input_tokens") * rates["cache_read"]
        + _tokens(usage, "output_tokens") * rates["output"]
    ) / 1_000_000


def _tokens(usage: dict, key: str) -> int:
    value = usage.get(key, 0)
    return value if isinstance(value, int) else 0


def billable_tokens(usage: dict) -> int:
    """Tokens that could carry a charge, whatever the rates happen to be."""
    five_minute, one_hour, unsplit = cache_write_tokens(usage)
    return (
        _tokens(usage, "input_tokens")
        + _tokens(usage, "output_tokens")
        + _tokens(usage, "cache_read_input_tokens")
        + five_minute + one_hour + unsplit
    )


def format_cost(cost: float) -> str:
    return f"${cost:.4f}" if cost < 1 else f"${cost:,.2f}"


def format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def baseline_model(config: dict, pricing: dict[str, dict], observed: set[str]) -> str | None:
    """The model to price the whole session at for the 'saved' counterfactual.

    Only models the session actually ran on are eligible. Taking the baseline from config
    (models.complex) instead invents a counterfactual that was never on the table: a session
    running entirely on a model exactly half the price of models.complex reports "saved 50%"
    whether or not a single prompt was ever routed, because the ratio, not the routing,
    produced the number. What can honestly be measured is what did run.
    """
    statusline = config.get("statusline")
    if isinstance(statusline, dict):
        explicit = statusline.get("savings_baseline")
        if isinstance(explicit, str) and explicit in pricing:
            return explicit
    candidates = [key for key in observed if key in pricing]
    if not candidates:
        return None
    # Sorted first so ties resolve identically whatever order the transcript was read in.
    return max(sorted(candidates), key=lambda key: pricing[key]["output"])


def summarise(entries: list[dict], pricing: dict[str, dict], config: dict, last_user_line: int) -> dict:
    totals = {
        "turn": 0.0, "session": 0.0, "baseline": 0.0, "tokens_in": 0, "tokens_out": 0,
        "unknown": set(), "baseline_model": None, "models": set(),
    }
    priced: list[dict] = []
    for entry in entries:
        usage = entry["usage"]
        totals["tokens_in"] += (
            _tokens(usage, "input_tokens")
            + _tokens(usage, "cache_creation_input_tokens")
            + _tokens(usage, "cache_read_input_tokens")
        )
        totals["tokens_out"] += _tokens(usage, "output_tokens")
        resolved = resolve_pricing(entry["model"], pricing)
        if resolved is None:
            # An entry with no billable tokens is not a gap in the total, so a missing rate for
            # it is not worth reporting. Claude Code writes <synthetic> placeholders shaped
            # exactly like this — interrupts and error messages, every token field zero — and
            # "no rate: <synthetic>" reads as missing cost data that the user cannot supply.
            if billable_tokens(usage):
                totals["unknown"].add(entry["model"] or "unknown")
            continue
        key, rates = resolved
        totals["models"].add(key)
        cost = usage_cost(usage, effective_rates(usage, rates))
        totals["session"] += cost
        if entry["line"] > last_user_line:
            totals["turn"] += cost
        priced.append(usage)

    if len(totals["models"]) >= MIN_MODELS_FOR_SAVINGS:
        key = baseline_model(config, pricing, totals["models"])
        if key is not None:
            rates = pricing[key]
            totals["baseline"] = sum(usage_cost(usage, effective_rates(usage, rates)) for usage in priced)
            totals["baseline_model"] = key
    return totals


def _routing_state(config: dict) -> tuple[bool, int]:
    """Read the two routing facts worth surfacing without depending on the hook."""
    routing = config.get("routing")
    enabled = routing.get("enabled", True) if isinstance(routing, dict) else True
    models = config.get("models")
    standard = models.get("standard") if isinstance(models, dict) else None
    tiers = 3 if isinstance(standard, str) and standard.strip() else 2
    requested = routing.get("tiers") if isinstance(routing, dict) else None
    if requested in (2, 3) and not isinstance(requested, bool):
        tiers = min(requested, tiers)
    return (enabled if isinstance(enabled, bool) else True), tiers


def _segment_turn(totals: dict, _config: dict) -> str:
    return f"turn {format_cost(totals['turn'])}"


def _segment_session(totals: dict, _config: dict) -> str:
    return f"session {format_cost(totals['session'])}"


def _segment_tokens(totals: dict, _config: dict) -> str:
    return f"{format_tokens(totals['tokens_in'])} in / {format_tokens(totals['tokens_out'])} out"


def _segment_saved(totals: dict, _config: dict) -> str | None:
    # What this session's own tokens would have cost had every one of them run on the dearest
    # model it actually used. Names that model, because a bare percentage invites the reader to
    # assume a baseline that was never measured. Silent when there is nothing real to report.
    saved = totals["baseline"] - totals["session"]
    if totals["baseline"] <= 0 or saved <= SAVINGS_MIN_USD:
        return None
    against = str(totals.get("baseline_model") or "").removeprefix("claude-")
    percent = saved / totals["baseline"] * 100
    return f"saved {format_cost(saved)} ({percent:.0f}%{f' vs {against}' if against else ''})"


def _segment_routing(_totals: dict, config: dict) -> str | None:
    enabled, tiers = _routing_state(config)
    if not enabled:
        return "routing off"
    # Two tiers is the default; saying so on every line would be noise.
    return "3 tiers" if tiers == 3 else None


def _segment_models(_totals: dict, config: dict) -> str | None:
    models = config.get("models")
    if not isinstance(models, dict):
        return None
    names = [str(models[key]) for key in ("simple", "standard", "complex") if isinstance(models.get(key), str)]
    return " > ".join(names) if names else None


SEGMENT_BUILDERS = {
    "turn": _segment_turn,
    "session": _segment_session,
    "saved": _segment_saved,
    "tokens": _segment_tokens,
    "routing": _segment_routing,
    "models": _segment_models,
}


def configured_segments(config: dict) -> tuple[str, ...]:
    statusline = config.get("statusline")
    requested = statusline.get("segments") if isinstance(statusline, dict) else None
    if not isinstance(requested, list) or not requested:
        return DEFAULT_SEGMENTS
    # Names arrive from a user-edited config, so they may not even be hashable.
    def known(name: object) -> bool:
        return isinstance(name, str) and name in SEGMENT_BUILDERS

    chosen = tuple(name for name in requested if known(name))
    unknown = [name for name in requested if not known(name)]
    if unknown:
        logger.warning("unknown statusline segment(s): %s", ", ".join(str(name) for name in unknown[:5]))
    return chosen or DEFAULT_SEGMENTS


def cost_segment(data: dict, config: dict) -> str:
    pricing = usable_pricing(config)
    if not pricing:
        return f"cost n/a: set pricing in {home_dir() / 'config.json'} (rates: {PRICING_URL})"

    transcript = data.get("transcript_path")
    entries: list[dict] = []
    last_user_line = -1
    if isinstance(transcript, str) and transcript:
        try:
            entries, last_user_line = collect_entries(Path(transcript))
        except OSError as exc:
            logger.warning("cannot read transcript: %s", exc)

    if not entries:
        builtin = (data.get("cost") or {}).get("total_cost_usd")
        if isinstance(builtin, (int, float)):
            return f"session ~{format_cost(float(builtin))} (builtin est.)"
        return "cost n/a: no usage data"

    totals = summarise(entries, pricing, config, last_user_line)
    parts = []
    for name in configured_segments(config):
        rendered = SEGMENT_BUILDERS[name](totals, config)
        if rendered:
            parts.append(rendered)
    if totals["unknown"]:
        parts.append(f"no rate: {', '.join(sorted(totals['unknown']))}")
    return " | ".join(parts) if parts else "cost n/a: no usage data"


def wrapped_line(config: dict, raw_stdin: str) -> str | None:
    """Run the statusline command that was installed before model-switcher and keep its output."""
    command = (config.get("statusline") or {}).get("wrap_command")
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            input=raw_stdin,
            capture_output=True,
            text=True,
            timeout=WRAP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("wrapped statusline failed: %s", exc)
        return None
    first = proc.stdout.splitlines()[0].strip() if proc.stdout else ""
    return first or None


def run(raw_stdin: str) -> str:
    try:
        data = json.loads(raw_stdin)
    except ValueError:
        return "model-switcher: no statusline data"
    if not isinstance(data, dict):
        return "model-switcher: no statusline data"
    config = load_config()
    base = wrapped_line(config, raw_stdin)
    if base is None:
        base = str((data.get("model") or {}).get("display_name") or "Claude")
    return f"{base} | {cost_segment(data, config)}"


def main() -> int:
    # The statusline must always print a line, whatever goes wrong.
    try:
        print(run(sys.stdin.read()))
    except Exception as exc:  # noqa: BLE001
        logger.warning("statusline failed: %s", exc)
        print("model-switcher: statusline error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
