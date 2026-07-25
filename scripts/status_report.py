"""The `status` report: what this install is configured to do, and what is wrong with it.

Splits into two kinds of check. The cheap ones live in complexity_router.health_warnings() and
are shared with the hook, which surfaces the BROKEN ones in-session. The expensive ones live
here, because they read the transcript corpus and nothing that does that belongs on the
interactive path.

Transcript content is untrusted: parsed with stdlib json only, never written anywhere, and the
one piece quoted back (a failed delegation's error) is truncated and shown only in this report.
"""

import json
from pathlib import Path

import complexity_router as router
import cost_statusline as statusline

# Enough history to tell "this never happens" from "this happened once"; few enough that the
# scan stays well under a second on a large corpus.
RECENT_SESSIONS = 20
MAX_ERROR_CHARS = 140
AGENT_PREFIXES = tuple(f"{prefix}-" for prefix in router.TIER_PREFIXES.values())


def _content_blocks(entry: dict) -> list:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def scan_transcripts(root: Path, limit: int = RECENT_SESSIONS) -> dict:
    """Which models generated, and how delegation actually went, over the most recent sessions."""
    result = {"sessions": 0, "models": {}, "delegations": {}}
    if not root.is_dir():
        return result
    try:
        files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    except OSError:
        return result

    for path in files:
        pending: dict = {}
        local_models: set = set()
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "assistant":
                    message = entry.get("message")
                    model = message.get("model") if isinstance(message, dict) else None
                    if isinstance(model, str) and model:
                        local_models.add(model)
                for block in _content_blocks(entry):
                    if not isinstance(block, dict):
                        continue
                    _note_delegation(block, pending, result["delegations"])
        result["sessions"] += 1
        for model in local_models:
            result["models"][model] = result["models"].get(model, 0) + 1
    return result


def _note_delegation(block: dict, pending: dict, delegations: dict) -> None:
    """Pair a Task call naming one of our agents with the result it eventually got."""
    if block.get("type") == "tool_use" and block.get("name") in ("Task", "Agent"):
        params = block.get("input")
        agent = params.get("subagent_type") if isinstance(params, dict) else None
        if isinstance(agent, str) and agent.startswith(AGENT_PREFIXES):
            pending[block.get("id")] = agent
            record = delegations.setdefault(agent, {"attempts": 0, "failures": 0, "last_error": None})
            record["attempts"] += 1
    elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
        agent = pending.pop(block.get("tool_use_id"))
        if block.get("is_error"):
            record = delegations[agent]
            record["failures"] += 1
            record["last_error"] = str(block.get("content"))[:MAX_ERROR_CHARS].strip()


def transcript_warnings(config: dict, scan: dict) -> list[tuple]:
    """Checks that need history, so they cannot run in the hook."""
    warnings: list[tuple] = []
    if not scan["sessions"]:
        return warnings

    for agent, record in sorted(scan["delegations"].items()):
        if record["failures"]:
            detail = f" Last error: {record['last_error']}" if record["last_error"] else ""
            warnings.append((router.BROKEN,
                f"{record['failures']} of {record['attempts']} recent delegations to '{agent}' "
                f"failed.{detail}"))

    routing = config.get("routing")
    enabled = routing.get("enabled", True) if isinstance(routing, dict) else True
    if enabled is not False and not scan["delegations"]:
        warnings.append((router.ADVICE,
            f"routing is on, but no prompt was delegated in the last {scan['sessions']} sessions — "
            "either nothing scored high enough, or the policy is not being followed"))

    pricing = statusline.usable_pricing(config)
    if pricing:
        unpriced = sorted(
            model for model in scan["models"]
            if not model.startswith("<") and statusline.resolve_pricing(model, pricing) is None
        )
        if unpriced:
            warnings.append((router.ADVICE,
                f"generated recently with no rate configured: {', '.join(unpriced)} — "
                "their cost is missing from every total"))
    return warnings


def _classifier_line(home: Path) -> str:
    try:
        data = json.loads((home / "classifier.json").read_text(encoding="utf-8"))
        scoring = data.get("scoring") or {}
        terms = scoring.get("terms") or {}
        corpus = data.get("corpus") or {}
        return (f"{len(terms)} terms, learned {str(data.get('generated_at', '?'))[:10]} from "
                f"{corpus.get('sessions', '?')} sessions / {corpus.get('prompts', '?')} prompts")
    except (OSError, ValueError, AttributeError):
        return "none — run 'model-switcher learn' to tune routing on your own history"


def _pricing_line(config: dict) -> str:
    pricing = statusline.usable_pricing(config)
    if not pricing:
        return "not configured — run 'model-switcher pricing --yes'"
    with_1h = sum(1 for rates in pricing.values() if statusline.CACHE_1H_KEY in rates)
    updated = config.get("pricing_updated")
    suffix = f", updated {updated}" if isinstance(updated, str) else ""
    return f"{len(pricing)} models{suffix}, {with_1h} with 1-hour cache rates"


def render_summary(config: dict, home: Path, session_model: object, echo=print) -> None:
    models = config.get("models") if isinstance(config.get("models"), dict) else {}
    routing = config.get("routing") if isinstance(config.get("routing"), dict) else {}
    ladder = ", ".join(
        f"{tier}={models[tier]}" for tier in ("simple", "standard", "complex") if models.get(tier)
    )
    echo(f"model-switcher   {home}")
    echo("")
    echo(f"  session model   {session_model or '(unknown)'}   (from settings.json)")
    echo(f"  routing         {'enabled' if routing.get('enabled', True) is not False else 'DISABLED'}")
    echo(f"  models          {ladder or '(unset)'}")
    echo(f"  pricing         {_pricing_line(config)}")
    echo(f"  classifier      {_classifier_line(home)}")
    echo("")


def render_checks(config: dict, home: Path, session_model: object, scan: dict, echo=print) -> int:
    """Print every finding. Returns how many were BROKEN, so the caller can set an exit code."""
    findings = router.health_warnings(config, session_model, home.parent / "agents")
    findings += transcript_warnings(config, scan)
    broken = [message for severity, message in findings if severity == router.BROKEN]
    advice = [message for severity, message in findings if severity == router.ADVICE]

    for message in broken:
        echo(f"  BROKEN  {message}")
    for message in advice:
        echo(f"  note    {message}")
    if not findings:
        echo(f"  ok      nothing wrong found (config, and {scan['sessions']} recent sessions)")
    return len(broken)
