"""Generate the heavy-task agent file with the configured model embedded in its name."""

import argparse
import json
import re
import sys
from pathlib import Path


def agent_name(model: str) -> str:
    # Keep in sync with complexity_router._heavy_agent_name: the directive must name this agent.
    suffix = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return f"heavy-task-{suffix}" if suffix else "heavy-task"


def install(source: Path, agents_dir: Path, model: str, manifest: dict) -> Path:
    name = agent_name(model)
    target = agents_dir / f"{name}.md"
    content = source.read_text(encoding="utf-8")
    content = re.sub(r"^name: .*$", f"name: {name}", content, count=1, flags=re.MULTILINE)
    content = re.sub(r"^model: .*$", f"model: {model}", content, count=1, flags=re.MULTILINE)

    _remove_previous(agents_dir, manifest, keep=target)
    agents_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    manifest["agent_file"] = str(target)
    return target


def uninstall(agents_dir: Path, manifest: dict) -> None:
    _remove_previous(agents_dir, manifest, keep=None)
    manifest.pop("agent_file", None)


def _remove_previous(agents_dir: Path, manifest: dict, keep: Path | None) -> None:
    candidates = {agents_dir / "heavy-task.md"}
    recorded = manifest.get("agent_file")
    if isinstance(recorded, str):
        candidates.add(Path(recorded))
    for path in candidates:
        # Only ever delete files this tool generated: heavy-task*.md inside the agents dir.
        if keep is not None and path == keep:
            continue
        if path.parent == agents_dir and path.name.startswith("heavy-task") and path.exists():
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
    args = parser.parse_args(argv)

    manifest = _load_json(args.manifest)
    if args.action == "install":
        if args.source is None:
            parser.error("--source is required for install")
        target = install(args.source, args.agents_dir, args.model, manifest)
        print(f"agent: {target} (model: {args.model})")
    else:
        uninstall(args.agents_dir, manifest)
        print("agent removed")

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
