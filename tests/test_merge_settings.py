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


class TestForeignHooksMentioningTheName:
    """The marker must match our installed scripts, not anything containing 'model-switcher'."""

    HOOK = {"hooks": [{"type": "command", "command": "echo my-model-switcher-logger ran"}]}

    def test_install_still_adds_our_hook_beside_it(self, paths):
        paths["settings"].write_text(json.dumps({"hooks": {"UserPromptSubmit": [self.HOOK]}}))
        run_cli("install", paths)
        matchers = read(paths["settings"])["hooks"]["UserPromptSubmit"]
        assert len(matchers) == 2
        assert any("complexity_router.py" in h["command"] for m in matchers for h in m["hooks"])

    def test_uninstall_leaves_the_user_hook_alone(self, paths):
        paths["settings"].write_text(json.dumps({"hooks": {"UserPromptSubmit": [self.HOOK]}}))
        run_cli("install", paths)
        run_cli("uninstall", paths)
        assert read(paths["settings"])["hooks"]["UserPromptSubmit"] == [self.HOOK]

    def test_a_user_hook_sharing_our_matcher_entry_survives_uninstall(self, paths):
        run_cli("install", paths)
        settings = read(paths["settings"])
        settings["hooks"]["UserPromptSubmit"][0]["hooks"].append({"type": "command", "command": "echo mine"})
        paths["settings"].write_text(json.dumps(settings))
        run_cli("uninstall", paths)
        remaining = read(paths["settings"])["hooks"]["UserPromptSubmit"]
        assert remaining == [{"hooks": [{"type": "command", "command": "echo mine"}]}]


class TestMalformedSettings:
    def test_invalid_json_fails_cleanly_and_touches_nothing(self, paths, capsys):
        paths["settings"].write_text("{not json")
        assert run_cli("install", paths) == 2
        err = capsys.readouterr().err
        assert "cannot parse" in err and len(err.strip().splitlines()) == 1
        assert paths["settings"].read_text() == "{not json"
        assert not paths["manifest"].exists()

    def test_a_non_object_settings_file_fails_cleanly(self, paths, capsys):
        paths["settings"].write_text("[1, 2]")
        assert run_cli("install", paths) == 2
        assert "JSON object" in capsys.readouterr().err

    def test_hooks_as_a_list_does_not_crash_install(self, paths):
        paths["settings"].write_text(json.dumps({"hooks": ["what"]}))
        assert run_cli("install", paths) == 0
        assert "UserPromptSubmit" in read(paths["settings"])["hooks"]

    def test_hooks_as_a_list_does_not_crash_uninstall(self, paths):
        paths["settings"].write_text(json.dumps({"hooks": ["what"]}))
        assert run_cli("uninstall", paths) == 0

    def test_a_matcher_entry_that_is_not_an_object_survives(self, paths):
        paths["settings"].write_text(json.dumps({"hooks": {"UserPromptSubmit": ["odd", {"hooks": "text"}]}}))
        assert run_cli("install", paths) == 0
        assert run_cli("uninstall", paths) == 0
        assert read(paths["settings"])["hooks"]["UserPromptSubmit"] == ["odd", {"hooks": "text"}]


class TestByteForByteRestore:
    def test_uninstall_restores_the_original_formatting(self, paths):
        original = '{\n    "model": "claude-fable-5"\n}\n'
        paths["settings"].write_text(original)
        run_cli("install", paths, set_model="sonnet")
        run_cli("uninstall", paths)
        assert paths["settings"].read_text() == original

    def test_post_install_changes_survive_and_win_over_the_backup(self, paths):
        paths["settings"].write_text('{"model": "claude-fable-5"}')
        run_cli("install", paths, set_model="sonnet")
        settings = read(paths["settings"])
        settings["theme"] = "dark"
        paths["settings"].write_text(json.dumps(settings))
        run_cli("uninstall", paths)
        assert read(paths["settings"]) == {"model": "claude-fable-5", "theme": "dark"}


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
