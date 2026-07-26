"""Install or remove model-switcher entries in Claude Code settings.json, tracked via a manifest."""

import argparse
import json
import shutil
import sys
from pathlib import Path

MARKER = "model-switcher"
# Entries are recognised by the scripts we install, under their model-switcher directory. Matching
# on the bare product name deleted user hooks that merely mentioned it and blocked ours from
# installing beside them.
OUR_SCRIPTS = ("complexity_router.py", "agent_router.py", "cost_statusline.py")


def hook_command(install_dir: Path) -> str:
    return f'python3 "{install_dir / "complexity_router.py"}"'


def agent_hook_command(install_dir: Path) -> str:
    return f'python3 "{install_dir / "agent_router.py"}"'


def statusline_command(install_dir: Path) -> str:
    return f'python3 "{install_dir / "cost_statusline.py"}"'


def _is_ours(command: str | None) -> bool:
    return isinstance(command, str) and any(f"{MARKER}/{name}" in command for name in OUR_SCRIPTS)


def _matcher_list(settings: dict, event: str) -> list:
    hooks = settings.get("hooks")
    matchers = hooks.get(event) if isinstance(hooks, dict) else None
    return matchers if isinstance(matchers, list) else []


def _hook_entries(matcher: object) -> list:
    entries = matcher.get("hooks") if isinstance(matcher, dict) else None
    return entries if isinstance(entries, list) else []


def _has_our_hook(settings: dict, event: str = "UserPromptSubmit") -> bool:
    return any(
        isinstance(hook, dict) and _is_ours(hook.get("command"))
        for matcher in _matcher_list(settings, event)
        for hook in _hook_entries(matcher)
    )


def _append_hook(settings: dict, event: str, entry: dict) -> None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        # A non-object hooks value is invalid for Claude Code anyway; the original is in the .bak.
        hooks = settings["hooks"] = {}
    matchers = hooks.get(event)
    if not isinstance(matchers, list):
        matchers = hooks[event] = []
    matchers.append(entry)


def _drop_our_hooks(settings: dict, event: str) -> None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get(event), list):
        return
    kept_matchers = []
    for matcher in hooks[event]:
        entries = _hook_entries(matcher)
        kept = [h for h in entries if not (isinstance(h, dict) and _is_ours(h.get("command")))]
        if len(kept) != len(entries):
            if not kept:
                continue  # the matcher entry existed only to carry our hook
            matcher = {**matcher, "hooks": kept}
        kept_matchers.append(matcher)
    if kept_matchers:
        hooks[event] = kept_matchers
    else:
        hooks.pop(event, None)


def install(settings: dict, manifest: dict, config: dict, install_dir: Path, set_model: str | None) -> None:
    if not _has_our_hook(settings):
        _append_hook(settings, "UserPromptSubmit",
                     {"hooks": [{"type": "command", "command": hook_command(install_dir)}]})
    # Matched on the Task tool so it runs only when Claude actually spawns an agent.
    if not _has_our_hook(settings, "PreToolUse"):
        _append_hook(settings, "PreToolUse",
                     {"matcher": "Task", "hooks": [{"type": "command", "command": agent_hook_command(install_dir)}]})

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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _restore_backup(settings_path: Path, backup: Path, expected: dict) -> bool:
    """Put the pre-install bytes back when uninstall lands on exactly the pre-install state,
    so a file we reformatted (indentation, key order) comes back untouched."""
    try:
        original = json.loads(backup.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if original != expected:
        return False
    shutil.copy2(backup, settings_path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--settings", required=True, type=Path)
    parser.add_argument("--install-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--set-model", default=None)
    args = parser.parse_args(argv)

    try:
        settings = _load_json(args.settings)
        manifest = _load_json(args.manifest)
        config = _load_json(args.config)
    except (OSError, ValueError) as exc:
        print(f"model-switcher: {exc}", file=sys.stderr)
        return 2

    backup = args.settings.with_name(args.settings.name + f".{MARKER}.bak")
    if args.settings.exists() and not backup.exists():
        shutil.copy2(args.settings, backup)

    if args.action == "install":
        install(settings, manifest, config, args.install_dir, args.set_model)
        _write_json(args.manifest, manifest)
        _write_json(args.config, config)
        _write_json(args.settings, settings)
    else:
        uninstall(settings, manifest)
        if not (backup.exists() and _restore_backup(args.settings, backup, settings)):
            _write_json(args.settings, settings)
    print(f"{args.action} complete: {args.settings}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
