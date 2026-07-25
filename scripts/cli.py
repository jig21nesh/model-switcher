"""Maintenance CLI for model-switcher.

Everything here is user-invoked and runs outside a Claude Code session, so unlike the
hook and the statusline it is allowed to take its time, print freely, and reach the
network when a subcommand explicitly asks for it.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import analyze_history
import complexity_router
import update_pricing


def home_dir() -> Path:
    # Same resolution order as the hook and the statusline, so all three agree on
    # which install they are talking about.
    return Path(os.environ.get("MODEL_SWITCHER_HOME", str(Path.home() / ".claude" / "model-switcher")))


def config_path() -> Path:
    return home_dir() / "config.json"


def cmd_pricing(args: argparse.Namespace) -> int:
    target = args.config or config_path()
    if not target.exists():
        print(f"no config at {target} — run ./install.sh first", file=sys.stderr)
        return 2
    forwarded = ["--config", str(target)]
    if args.offline:
        forwarded.append("--offline")
    if args.source:
        forwarded += ["--source", args.source]
    if args.yes:
        forwarded.append("--yes")
    return update_pricing.main(forwarded)


def configured_threshold(default: float = 5.0) -> float:
    """Read the user's own threshold so the learn report measures the routing they actually run."""
    try:
        config = json.loads(config_path().read_text(encoding="utf-8"))
        value = config["complexity"]["threshold"]
    except (OSError, ValueError, KeyError, TypeError):
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def cmd_learn(args: argparse.Namespace) -> int:
    home = home_dir()
    if not home.is_dir():
        print(f"no install at {home} — run ./install.sh first", file=sys.stderr)
        return 2
    forwarded = [
        "--home", str(home),
        "--threshold", str(args.threshold if args.threshold is not None else configured_threshold()),
        "--generated-at", datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    ]
    for directory in args.transcripts or []:
        forwarded += ["--transcripts", str(directory)]
    if args.max_sessions is not None:
        forwarded += ["--max-sessions", str(args.max_sessions)]
    if args.apply:
        forwarded.append("--apply")
    return analyze_history.main(forwarded)


def cmd_explain(args: argparse.Namespace) -> int:
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print("nothing to score", file=sys.stderr)
        return 2

    config = complexity_router.load_config()
    classifier = {} if args.no_classifier else complexity_router.load_classifier()
    detail = complexity_router.analyse_prompt(prompt, classifier)
    threshold = complexity_router.threshold_from(config)

    print(f'prompt: "{prompt[:120]}{"..." if len(prompt) > 120 else ""}"\n')
    if detail["signals"]:
        for name, points in detail["signals"]:
            print(f"  {name:<44}{points:>+5}")
    else:
        print(f"  {'no built-in signals matched':<44}{0:>5}")
    print(f"  {'':<44}{'-' * 5}")
    print(f"  {'built-in score':<44}{detail['base']:>5}")

    if classifier:
        matched = sorted(detail["matched_terms"].items(), key=lambda item: -abs(item[1]))
        shown = ", ".join(f"{term} {weight:+.2f}" for term, weight in matched[:8]) or "none matched"
        print(f"  {'learned terms':<44}{detail['learned']:>+5.1f}")
        print(f"    {shown}")
    else:
        print("  (no learned classifier — run: model-switcher learn)")
    if detail["caps"]:
        print(f"  capped to 2 by: {', '.join(detail['caps'])}")

    score = detail["score"]
    tier = complexity_router.select_tier(score, config)
    if tier is None:
        verdict = "answered in-session"
    else:
        model = config.get("models", {}).get(tier, "?")
        verdict = f"{complexity_router.TIER_LABELS[tier]} -> {complexity_router.agent_name_for(model, tier)}"
    print(f"\n  score {score}/10   threshold {threshold:g}   {verdict}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="model-switcher", description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)

    pricing = subcommands.add_parser(
        "pricing",
        help="compare your pricing table against the maintained one, and optionally update it",
    )
    pricing.add_argument("--config", type=Path, default=None, help="config.json to check (default: your install)")
    pricing.add_argument("--source", default=None, help="HTTPS URL of the pricing table")
    pricing.add_argument("--offline", action="store_true", help="use the table bundled in this repo")
    pricing.add_argument("--yes", action="store_true", help="apply the changes (default is check-only)")
    pricing.set_defaults(handler=cmd_pricing)

    learn = subcommands.add_parser(
        "learn",
        help="learn routing weights from which of your past prompts became real work",
    )
    learn.add_argument(
        "--transcripts", type=Path, action="append", default=None,
        help=f"directory of transcripts, repeatable (default: {analyze_history.DEFAULT_TRANSCRIPTS})",
    )
    learn.add_argument("--max-sessions", type=int, default=None, help="cap how many sessions are read")
    learn.add_argument("--threshold", type=float, default=None, help="threshold to measure against")
    learn.add_argument("--apply", action="store_true", help="promote the candidate to the live classifier")
    learn.set_defaults(handler=cmd_learn)

    explain = subcommands.add_parser(
        "explain",
        help="show how a prompt scores and where it would route, without spending a token",
    )
    explain.add_argument("prompt", nargs="+", help="the prompt to score")
    explain.add_argument(
        "--no-classifier", action="store_true", help="score with the built-in signals only"
    )
    explain.set_defaults(handler=cmd_explain)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
