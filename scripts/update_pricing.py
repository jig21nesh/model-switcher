"""Refresh the pricing table in the user's config from the maintained table in this repo.

Deliberately separate from the hook and the statusline: those two never make network
calls. This is a user-invoked maintenance command. It validates every rate before it
writes anything, keeps a backup, and leaves models it does not know about alone.
"""

import argparse
import json
import math
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCHEMA_VERSION = 1
DEFAULT_SOURCE = "https://raw.githubusercontent.com/jig21nesh/model-switcher/main/config/pricing.json"
BUNDLED = Path(__file__).resolve().parent.parent / "config" / "pricing.json"
FETCH_TIMEOUT_SECONDS = 15
# The real table is a few KB; anything far larger is not the file we asked for.
MAX_SOURCE_BYTES = 256 * 1024
REQUIRED_RATES = ("input", "output", "cache_write", "cache_read")
OPTIONAL_RATES = ("cache_write_1h",)
MAX_RATE_USD_PER_MTOK = 10_000.0
MODEL_ID_RE = re.compile(r"[A-Za-z0-9._\[\]-]{1,64}")


class PricingError(Exception):
    """The source table could not be fetched, parsed, or trusted."""


def _is_rate(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= MAX_RATE_USD_PER_MTOK
    )


def _validate_rates(model: str, rates: object, *, nested: bool = False) -> dict:
    if not isinstance(rates, dict):
        raise PricingError(f"{model}: rate block is not an object")
    missing = [key for key in REQUIRED_RATES if key not in rates]
    if missing:
        raise PricingError(f"{model}: missing rate(s) {', '.join(missing)}")
    clean = {}
    for key in (*REQUIRED_RATES, *OPTIONAL_RATES):
        if key in rates:
            if not _is_rate(rates[key]):
                raise PricingError(f"{model}: rate '{key}' is not a usable number")
            clean[key] = float(rates[key])
    if not nested and isinstance(rates.get("fast"), dict):
        clean["fast"] = _validate_rates(f"{model}.fast", rates["fast"], nested=True)
    return clean


def validate_source(payload: object) -> dict:
    """Return {model_id: rates} from a source table, or raise PricingError."""
    if not isinstance(payload, dict):
        raise PricingError("source table is not a JSON object")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PricingError(f"unsupported schema_version {version!r} (expected {SCHEMA_VERSION})")
    models = payload.get("models")
    if not isinstance(models, dict) or not models:
        raise PricingError("source table has no models")
    validated = {}
    for model, rates in models.items():
        if not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model):
            raise PricingError(f"implausible model id {model!r}")
        validated[model] = _validate_rates(model, rates)
    return validated


def fetch_source(url: str) -> dict:
    if not url.lower().startswith("https://"):
        raise PricingError(f"refusing to fetch pricing over a non-HTTPS URL: {url}")
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read(MAX_SOURCE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PricingError(f"cannot fetch {url}: {exc}") from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise PricingError(f"source table is larger than {MAX_SOURCE_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PricingError(f"source table is not valid JSON: {exc}") from exc


def read_source(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PricingError(f"cannot read {path}: {exc}") from exc


def _flatten(rates: dict) -> dict[str, float]:
    flat = {}
    for key, value in rates.items():
        if isinstance(value, dict):
            flat.update({f"{key}.{inner}": number for inner, number in value.items()})
        else:
            flat[key] = value
    return flat


def diff(current: object, source: dict[str, dict]) -> tuple[list[str], list[tuple[str, str, object, float]]]:
    """Return (models absent from the config, per-rate changes) against the source table."""
    existing = current if isinstance(current, dict) else {}
    added, changed = [], []
    for model, rates in sorted(source.items()):
        have = existing.get(model)
        if not isinstance(have, dict):
            added.append(model)
            continue
        have_flat, want_flat = _flatten(have), _flatten(rates)
        for key, want in sorted(want_flat.items()):
            got = have_flat.get(key)
            if not isinstance(got, (int, float)) or isinstance(got, bool) or float(got) != want:
                changed.append((model, key, got, want))
    return added, changed


def render(added: list[str], changed: list, source_models: dict, config_models: object) -> str:
    lines = []
    for model in added:
        rates = source_models[model]
        summary = " / ".join(f"{key} {value:g}" for key, value in _flatten(rates).items())
        lines.append(f"  + {model}: {summary}")
    for model, key, got, want in changed:
        shown = "absent" if got is None else f"{float(got):g}"
        lines.append(f"  ~ {model}.{key}: {shown} -> {want:g}")
    extra = sorted(set(config_models) - set(source_models)) if isinstance(config_models, dict) else []
    for model in extra:
        lines.append(f"  = {model}: not in the source table, left untouched")
    return "\n".join(lines)


def apply(config: dict, source_models: dict[str, dict], meta: dict) -> dict:
    """Merge source rates into the config, preserving models the source does not know."""
    pricing = config.get("pricing_usd_per_mtok")
    merged = dict(pricing) if isinstance(pricing, dict) else {}
    merged.update({model: dict(rates) for model, rates in source_models.items()})
    config["pricing_usd_per_mtok"] = merged
    for key in ("updated", "source", "notes"):
        value = meta.get(key)
        if isinstance(value, (str, list)) and value:
            target = {"updated": "pricing_updated", "source": "pricing_reference", "notes": "pricing_notes"}[key]
            config[target] = " ".join(value) if isinstance(value, list) else value
    return config


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="the config.json to update")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="HTTPS URL of the pricing table")
    parser.add_argument("--offline", action="store_true", help="use the table bundled in this repo")
    parser.add_argument("--bundled", type=Path, default=BUNDLED, help=argparse.SUPPRESS)
    parser.add_argument("--yes", action="store_true", help="apply the changes (default is check-only)")
    args = parser.parse_args(argv)

    try:
        payload = read_source(args.bundled) if args.offline else fetch_source(args.source)
        source_models = validate_source(payload)
    except PricingError as exc:
        print(f"pricing update failed: {exc}", file=sys.stderr)
        return 2

    try:
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config.exists() else {}
    except ValueError as exc:
        print(f"cannot parse {args.config}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(config, dict):
        print(f"{args.config} does not contain a JSON object", file=sys.stderr)
        return 2

    origin = args.bundled if args.offline else args.source
    added, changed = diff(config.get("pricing_usd_per_mtok"), source_models)
    if not added and not changed:
        print(f"pricing is up to date with {origin}")
        return 0

    print(f"pricing differs from {origin}:")
    print(render(added, changed, source_models, config.get("pricing_usd_per_mtok")))
    if not args.yes:
        print(f"\n{len(added)} model(s) to add, {len(changed)} rate(s) to change. Re-run with --yes to apply.")
        return 1

    backup = args.config.with_name(args.config.name + ".pricing.bak")
    if args.config.exists():
        shutil.copy2(args.config, backup)
    _write_json(args.config, apply(config, source_models, payload))
    print(f"\napplied to {args.config}" + (f" (backup: {backup})" if backup.exists() else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
