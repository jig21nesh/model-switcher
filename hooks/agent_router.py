"""PreToolUse hook on Task: apply the routing policy to agent spawns, not only to prompts.

The UserPromptSubmit hook scores what *you* type. It never sees what Claude then delegates:
subagent prompts are sidechains and do not pass through it. So a policy that says "work at this
level runs on the heavy tier" held for your prompts and was silently ignored the moment Claude
fanned out to its own agents, which is where a large share of the tokens actually go.

This closes that gap by scoring the agent's own prompt with the same scorer and rewriting
`subagent_type` when a generic agent has been handed work the policy says belongs on a
configured tier. It only ever *upgrades* an unspecialised agent onto a tier; it never overrides
an agent chosen deliberately, and never downgrades, because a caller that asked for a specific
agent knows something the score does not.

Same contract as the prompt router: fails open. Any error, any doubt, exit 0 with no output and
let the tool call proceed exactly as Claude wrote it.
"""

import json
import logging
import sys
from pathlib import Path

import complexity_router as router

logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="model-switcher %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Agents with no model or speciality of their own — they inherit the session model, so handing
# them tiered work quietly opts out of routing. Explore is deliberately excluded: it is a cheap,
# read-only search agent, and promoting it to the heavy tier would spend a lot to do a little.
DEFAULT_GENERIC_AGENTS = ("general-purpose", "claude")
# Scoring an enormous delegated prompt adds nothing and this runs on the interactive path.
PROMPT_MAX_CHARS = router.SCORE_MAX_CHARS


def generic_agents(config: dict) -> tuple:
    routing = config.get("routing")
    configured = routing.get("generic_agents") if isinstance(routing, dict) else None
    if isinstance(configured, list):
        names = tuple(name for name in configured if isinstance(name, str) and name.strip())
        if names:
            return names
    return DEFAULT_GENERIC_AGENTS


def agent_routing_enabled(config: dict) -> bool:
    """Off whenever prompt routing is off: one switch, no surprising half-on state."""
    routing = config.get("routing")
    if not isinstance(routing, dict):
        return True
    if routing.get("enabled") is False:
        return False
    enabled = routing.get("agents", True)
    return enabled if isinstance(enabled, bool) else True


def agent_exists(name: str, agents_dir: Path) -> bool:
    try:
        return (agents_dir / f"{name}.md").is_file()
    except OSError:
        return False


def decide(tool_input: dict, config: dict, agents_dir: Path) -> tuple[str, str] | None:
    """The agent to run instead, and why. None leaves the call untouched."""
    requested = tool_input.get("subagent_type")
    if not isinstance(requested, str) or requested not in generic_agents(config):
        return None
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None

    score = router.score_prompt(prompt[:PROMPT_MAX_CHARS], router.load_classifier())
    tier = router.select_tier(score, config)
    if tier is None:
        return None

    model = config.get("models", {}).get(tier) if isinstance(config.get("models"), dict) else None
    if not isinstance(model, str) or not router.MODEL_NAME_RE.fullmatch(model):
        return None
    target = router.agent_name_for(model, tier)
    # Rewriting to an agent that does not exist would turn a working call into a failing one.
    if target == requested or not agent_exists(target, agents_dir):
        return None
    edge = router.threshold_from(config) if tier == "complex" else router.standard_threshold_from(config)
    reason = (
        f"[model-switcher] This delegated task scores {score}/10 (threshold {edge:g}), which the "
        f"routing policy classifies {router.TIER_LABELS[tier]}. '{requested}' inherits the session "
        f"model, so it was rewritten to '{target}' (configured model: {model}). Set "
        '"routing": {"agents": false} in config.json to stop this.'
    )
    return target, reason


def run(stdin_text: str) -> str:
    try:
        payload = json.loads(stdin_text)
    except ValueError:
        return ""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Task":
        return ""
    # Only top-level spawns. Inside a subagent this would let a tier agent promote its own
    # helpers, compounding cost a level down where nobody is watching.
    if payload.get("agent_id"):
        return ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""

    config = router.load_config()
    if not agent_routing_enabled(config) or not router.models_configured(config):
        return ""

    outcome = decide(tool_input, config, router.claude_dir() / "agents")
    if outcome is None:
        return ""
    target, reason = outcome
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {**tool_input, "subagent_type": target},
        "additionalContext": reason,
    }})


def main() -> int:
    # Never block a tool call because this hook had a bad day.
    try:
        output = run(sys.stdin.read())
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent router failed, leaving the call alone: %s", exc)
        return 0
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
