import json
from pathlib import Path

import pytest

import merge_settings

INSTALL_DIR = Path("/home/user/.claude/model-switcher")


@pytest.fixture
def paths(tmp_path):
    return {
        "settings": tmp_path / "settings.json",
        "config": tmp_path / "config.json",
        "manifest": tmp_path / "installed.json",
    }


def run_cli(action, paths, set_model=None):
    argv = [
        action,
        "--settings", str(paths["settings"]),
        "--install-dir", str(INSTALL_DIR),
        "--config", str(paths["config"]),
        "--manifest", str(paths["manifest"]),
    ]
    if set_model:
        argv += ["--set-model", set_model]
    return merge_settings.main(argv)


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


class TestInstall:
    def test_install_into_empty_settings(self, paths):
        run_cli("install", paths, set_model="sonnet")
        settings = read(paths["settings"])
        hook = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        assert "complexity_router.py" in hook["command"]
        assert "cost_statusline.py" in settings["statusLine"]["command"]
        assert settings["model"] == "sonnet"

    def test_install_is_idempotent(self, paths):
        run_cli("install", paths, set_model="sonnet")
        run_cli("install", paths, set_model="sonnet")
        settings = read(paths["settings"])
        assert len(settings["hooks"]["UserPromptSubmit"]) == 1
        assert read(paths["manifest"]).get("previous_model") is None

    def test_existing_hooks_preserved(self, paths):
        other_hook = {"hooks": [{"type": "command", "command": "echo other"}]}
        paths["settings"].write_text(json.dumps({"hooks": {"UserPromptSubmit": [other_hook]}}))
        run_cli("install", paths)
        matchers = read(paths["settings"])["hooks"]["UserPromptSubmit"]
        assert len(matchers) == 2
        assert matchers[0] == other_hook

    def test_existing_statusline_wrapped_and_recorded(self, paths):
        previous = {"type": "command", "command": "bash /home/user/statusline.sh"}
        paths["settings"].write_text(json.dumps({"statusLine": previous, "model": "claude-fable-5"}))
        run_cli("install", paths, set_model="sonnet")
        assert read(paths["manifest"])["previous_statusline"] == previous
        assert read(paths["manifest"])["previous_model"] == "claude-fable-5"
        assert read(paths["config"])["statusline"]["wrap_command"] == previous["command"]
        assert "cost_statusline.py" in read(paths["settings"])["statusLine"]["command"]

    def test_backup_created_once(self, paths):
        paths["settings"].write_text(json.dumps({"model": "claude-fable-5"}))
        run_cli("install", paths, set_model="sonnet")
        backup = paths["settings"].with_name(paths["settings"].name + ".model-switcher.bak")
        assert read(backup) == {"model": "claude-fable-5"}
        run_cli("install", paths, set_model="sonnet")
        assert read(backup) == {"model": "claude-fable-5"}

    def test_skip_model_leaves_model_untouched(self, paths):
        paths["settings"].write_text(json.dumps({"model": "claude-fable-5"}))
        run_cli("install", paths)
        assert read(paths["settings"])["model"] == "claude-fable-5"


class TestUninstall:
    def test_uninstall_restores_previous_state(self, paths):
        previous_status = {"type": "command", "command": "bash /home/user/statusline.sh"}
        paths["settings"].write_text(json.dumps({"statusLine": previous_status, "model": "claude-fable-5"}))
        run_cli("install", paths, set_model="sonnet")
        run_cli("uninstall", paths)
        settings = read(paths["settings"])
        assert settings["statusLine"] == previous_status
        assert settings["model"] == "claude-fable-5"
        assert "hooks" not in settings

    def test_uninstall_keeps_foreign_entries(self, paths):
        other_hook = {"hooks": [{"type": "command", "command": "echo other"}]}
        paths["settings"].write_text(json.dumps({"hooks": {"UserPromptSubmit": [other_hook]}}))
        run_cli("install", paths)
        run_cli("uninstall", paths)
        assert read(paths["settings"])["hooks"]["UserPromptSubmit"] == [other_hook]

    def test_uninstall_respects_user_model_change(self, paths):
        paths["settings"].write_text(json.dumps({"model": "claude-fable-5"}))
        run_cli("install", paths, set_model="sonnet")
        settings = read(paths["settings"])
        settings["model"] = "opus"
        paths["settings"].write_text(json.dumps(settings))
        run_cli("uninstall", paths)
        assert read(paths["settings"])["model"] == "opus"

    def test_uninstall_without_install_is_safe(self, paths):
        paths["settings"].write_text(json.dumps({"model": "claude-fable-5"}))
        run_cli("uninstall", paths)
        assert read(paths["settings"]) == {"model": "claude-fable-5"}

    def test_uninstall_removes_model_when_none_before(self, paths):
        run_cli("install", paths, set_model="sonnet")
        run_cli("uninstall", paths)
        assert "model" not in read(paths["settings"])
