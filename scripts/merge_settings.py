"""Install or remove model-switcher entries in Claude Code settings.json, tracked via a manifest."""

import argparse
import json
import shutil
import sys
from pathlib import Path

MARKER = "model-switcher"


def hook_command(install_dir: Path) -> str:
    return f'python3 "{install_dir / "complexity_router.py"}"'


def agent_hook_command(install_dir: Path) -> str:
    return f'python3 "{install_dir / "agent_router.py"}"'


def statusline_command(install_dir: Path) -> str:
    return f'python3 "{install_dir / "cost_statusline.py"}"'


def _is_ours(command: str | None) -> bool:
    return isinstance(command, str) and MARKER in command


def _has_our_hook(settings: dict, event: str = "UserPromptSubmit") -> bool:
    for matcher in settings.get("hooks", {}).get(event, []):
        if not isinstance(matcher, dict):
            continue
        for hook in matcher.get("hooks", []):
            if isinstance(hook, dict) and _is_ours(hook.get("command")):
                return True
    return False


def _drop_our_hooks(settings: dict, event: str) -> None:
    matchers = settings.get("hooks", {}).get(event)
    if not isinstance(matchers, list):
        return
    kept = [
        m for m in matchers
        if not (isinstance(m, dict) and any(
            isinstance(h, dict) and _is_ours(h.get("command")) for h in m.get("hooks", [])
        ))
    ]
    if kept:
        settings["hooks"][event] = kept
    else:
        settings["hooks"].pop(event, None)


def install(settings: dict, manifest: dict, config: dict, install_dir: Path, set_model: str | None) -> None:
    if not _has_our_hook(settings):
        settings.setdefault("hooks", {}).setdefault("UserPromptSubmit", []).append(
            {"hooks": [{"type": "command", "command": hook_command(install_dir)}]}
        )
    # Matched on the Task tool so it runs only when Claude actually spawns an agent.
    if not _has_our_hook(settings, "PreToolUse"):
        settings.setdefault("hooks", {}).setdefault("PreToolUse", []).append(
            {"matcher": "Task", "hooks": [{"type": "command", "command": agent_hook_command(install_dir)}]}
        )

    current_statusline = settings.get("statusLine")
    if not (isinstance(current_statusline, dict) and _is_ours(current_statusline.get("command"))):
        if isinstance(current_statusline, dict):
            manifest.setdefault("previous_statusline", current_statusline)
            if current_statusline.get("type") == "command" and current_statusline.get("command"):
                config.setdefault("statusline", {})["wrap_command"] = current_statusline["command"]
        settings["statusLine"] = {"type": "command", "command": statusline_command(install_dir)}

    if set_model and settings.get("model") != set_model:
        if "previous_model" not in manifest:
            manifest["previous_model"] = settings.get("model")
        manifest["set_model"] = set_model
        settings["model"] = set_model


def uninstall(settings: dict, manifest: dict) -> None:
    _drop_our_hooks(settings, "UserPromptSubmit")
    _drop_our_hooks(settings, "PreToolUse")
    if isinstance(settings.get("hooks"), dict) and not settings["hooks"]:
        settings.pop("hooks")

    current_statusline = settings.get("statusLine")
    if isinstance(current_statusline, dict) and _is_ours(current_statusline.get("command")):
        previous = manifest.get("previous_statusline")
        if isinstance(previous, dict):
            settings["statusLine"] = previous
        else:
            settings.pop("statusLine", None)

    if manifest.get("set_model") and settings.get("model") == manifest["set_model"]:
        if manifest.get("previous_model"):
            settings["model"] = manifest["previous_model"]
        else:
            settings.pop("model", None)


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--install-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--set-model", default=None)
    args = parser.parse_args(argv)

    settings = _load_json(args.settings)
    manifest = _load_json(args.manifest)
    config = _load_json(args.config)

    backup = args.settings.with_name(args.settings.name + f".{MARKER}.bak")
    if args.settings.exists() and not backup.exists():
        shutil.copy2(args.settings, backup)

    if args.action == "install":
        install(settings, manifest, config, args.install_dir, args.set_model)
        _write_json(args.manifest, manifest)
        _write_json(args.config, config)
    else:
        uninstall(settings, manifest)

    _write_json(args.settings, settings)
    print(f"{args.action} complete: {args.settings}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
