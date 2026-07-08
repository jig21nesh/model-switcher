"""Install or remove the model-switcher managed block in the user's global CLAUDE.md."""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

BLOCK_RE = re.compile(r"[ \t]*<!-- model-switcher:begin.*?model-switcher:end -->\n?", re.DOTALL)
BACKUP_SUFFIX = ".model-switcher.bak"


def install(claude_md: Path, block: str, manifest: dict) -> str:
    if not claude_md.exists():
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        claude_md.write_text(block, encoding="utf-8")
        manifest["created_claude_md"] = True
        return "created"

    content = claude_md.read_text(encoding="utf-8")
    backup = claude_md.with_name(claude_md.name + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(claude_md, backup)
    manifest.setdefault("created_claude_md", False)

    if BLOCK_RE.search(content):
        content = BLOCK_RE.sub("", content).rstrip("\n")
        claude_md.write_text(f"{content}\n\n{block}", encoding="utf-8")
        return "updated"
    claude_md.write_text(f"{content.rstrip()}\n\n{block}", encoding="utf-8")
    return "appended"


def uninstall(claude_md: Path, manifest: dict) -> str:
    if not claude_md.exists():
        return "absent"
    content = claude_md.read_text(encoding="utf-8")
    if not BLOCK_RE.search(content):
        return "no block"
    remaining = BLOCK_RE.sub("", content)
    if manifest.get("created_claude_md") and not remaining.strip():
        claude_md.unlink()
        return "removed file (was created by installer)"
    claude_md.write_text(remaining.rstrip("\n") + "\n" if remaining.strip() else "", encoding="utf-8")
    return "block removed"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--claude-md", required=True, type=Path)
    parser.add_argument("--block-file", type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = _load_json(args.manifest)
    if args.action == "install":
        if args.block_file is None:
            parser.error("--block-file is required for install")
        block = args.block_file.read_text(encoding="utf-8").strip() + "\n"
        result = install(args.claude_md, block, manifest)
    else:
        result = uninstall(args.claude_md, manifest)

    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"CLAUDE.md {args.action}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
