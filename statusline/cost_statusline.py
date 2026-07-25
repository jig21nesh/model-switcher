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


def match_pricing(model_id: str, pricing: dict[str, dict]) -> dict | None:
    if model_id in pricing:
        return pricing[model_id]
    # Dated releases (claude-sonnet-5-20250929) match their base entry (claude-sonnet-5).
    prefixes = [key for key in pricing if model_id.startswith(key)]
    if prefixes:
        return pricing[max(prefixes, key=len)]
    return None


def parse_transcript(path: Path) -> tuple[list[dict], int]:
    """Return deduped assistant usage entries and the line number of the last real user message."""
    entries: dict[str, dict] = {}
    last_user_line = -1
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
            elif obj.get("type") == "assistant":
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                # Streaming rewrites the same message id; the last entry carries final usage.
                msg_id = message.get("id") or obj.get("uuid") or f"line-{i}"
                entries[msg_id] = {"model": str(message.get("model") or ""), "usage": usage, "line": i}
    return list(entries.values()), last_user_line


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


def format_cost(cost: float) -> str:
    return f"${cost:.4f}" if cost < 1 else f"${cost:,.2f}"


def format_tokens(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def cost_segment(data: dict, config: dict) -> str:
    pricing = usable_pricing(config)
    if not pricing:
        return f"cost n/a: set pricing in {home_dir() / 'config.json'} (rates: {PRICING_URL})"

    transcript = data.get("transcript_path")
    entries: list[dict] = []
    last_user_line = -1
    if isinstance(transcript, str) and transcript:
        try:
            entries, last_user_line = parse_transcript(Path(transcript))
        except OSError as exc:
            logger.warning("cannot read transcript: %s", exc)

    if not entries:
        builtin = (data.get("cost") or {}).get("total_cost_usd")
        if isinstance(builtin, (int, float)):
            return f"session ~{format_cost(float(builtin))} (builtin est.)"
        return "cost n/a: no usage data"

    session_cost = turn_cost = 0.0
    tokens_in = tokens_out = 0
    unknown_models: set[str] = set()
    for entry in entries:
        usage = entry["usage"]
        tokens_in += (
            _tokens(usage, "input_tokens")
            + _tokens(usage, "cache_creation_input_tokens")
            + _tokens(usage, "cache_read_input_tokens")
        )
        tokens_out += _tokens(usage, "output_tokens")
        rates = match_pricing(entry["model"], pricing)
        if rates is None:
            unknown_models.add(entry["model"] or "unknown")
            continue
        cost = usage_cost(usage, effective_rates(usage, rates))
        session_cost += cost
        if entry["line"] > last_user_line:
            turn_cost += cost

    segment = (
        f"turn {format_cost(turn_cost)} | session {format_cost(session_cost)} "
        f"({format_tokens(tokens_in)} in / {format_tokens(tokens_out)} out)"
    )
    if unknown_models:
        segment += f" | no rate: {', '.join(sorted(unknown_models))}"
    return segment


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
