"""Generate a tier's agent file with the configured model embedded in its name."""

import argparse
import json
import re
import sys
from pathlib import Path

# The complex tier keeps its original prefix and manifest key so that installs, manifests and
# CLAUDE.md policy blocks written before the middle tier existed stay valid.
TIERS = {
    "complex": {"prefix": "heavy-task", "manifest_key": "agent_file"},
    "standard": {"prefix": "mid-task", "manifest_key": "standard_agent_file"},
}


def agent_name(model: str, tier: str = "complex") -> str:
    # Keep in sync with complexity_router.agent_name_for: the directive must name this agent.
    prefix = TIERS[tier]["prefix"]
    suffix = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return f"{prefix}-{suffix}" if suffix else prefix


def install(source: Path, agents_dir: Path, model: str, manifest: dict, tier: str = "complex") -> Path:
    name = agent_name(model, tier)
    target = agents_dir / f"{name}.md"
    content = source.read_text(encoding="utf-8")
    content = re.sub(r"^name: .*$", f"name: {name}", content, count=1, flags=re.MULTILINE)
    content = re.sub(r"^model: .*$", f"model: {model}", content, count=1, flags=re.MULTILINE)

    _remove_previous(agents_dir, manifest, tier, keep=target)
    agents_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    manifest[TIERS[tier]["manifest_key"]] = str(target)
    return target


def uninstall(agents_dir: Path, manifest: dict, tier: str | None = None) -> None:
    for name in (tier,) if tier else tuple(TIERS):
        _remove_previous(agents_dir, manifest, name, keep=None)
        manifest.pop(TIERS[name]["manifest_key"], None)


def _remove_previous(agents_dir: Path, manifest: dict, tier: str, keep: Path | None) -> None:
    prefix = TIERS[tier]["prefix"]
    candidates = {agents_dir / f"{prefix}.md"}
    recorded = manifest.get(TIERS[tier]["manifest_key"])
    if isinstance(recorded, str):
        candidates.add(Path(recorded))
    for path in candidates:
        # Only ever delete files this tool generated: <prefix>*.md inside the agents dir.
        if keep is not None and path == keep:
            continue
        if path.parent == agents_dir and path.name.startswith(prefix) and path.exists():
            path.unlink()


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--agents-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--model", default="fable")
    parser.add_argument(
        "--tier",
        choices=tuple(TIERS),
        default=None,
        help="which tier's agent to manage (install defaults to complex; uninstall to all tiers)",
    )
    args = parser.parse_args(argv)

    manifest = _load_json(args.manifest)
    if args.action == "install":
        if args.source is None:
            parser.error("--source is required for install")
        tier = args.tier or "complex"
        target = install(args.source, args.agents_dir, args.model, manifest, tier)
        print(f"agent:      {target} ({tier} tier, model: {args.model})")
    else:
        uninstall(args.agents_dir, manifest, args.tier)
        print(f"agent removed ({args.tier or 'all tiers'})")

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
