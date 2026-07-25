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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
