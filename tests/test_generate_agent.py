import json
from pathlib import Path

import pytest

import generate_agent

SOURCE = Path(__file__).resolve().parent.parent / "agents" / "heavy-task.md"


@pytest.fixture
def paths(tmp_path):
    return {"agents": tmp_path / "agents", "manifest": tmp_path / "installed.json"}


def run_cli(action, paths, model=None):
    argv = [action, "--agents-dir", str(paths["agents"]), "--manifest", str(paths["manifest"])]
    if action == "install":
        argv += ["--source", str(SOURCE), "--model", model or "opus"]
    return generate_agent.main(argv)


def manifest(paths):
    return json.loads(paths["manifest"].read_text()) if paths["manifest"].exists() else {}


class TestAgentName:
    def test_alias(self):
        assert generate_agent.agent_name("opus") == "heavy-task-opus"

    def test_full_model_id(self):
        assert generate_agent.agent_name("claude-opus-4-8") == "heavy-task-claude-opus-4-8"

    def test_special_chars_sanitised(self):
        assert generate_agent.agent_name("opus[1m]") == "heavy-task-opus-1m"

    def test_empty_model_falls_back(self):
        assert generate_agent.agent_name("") == "heavy-task"


class TestInstall:
    def test_writes_agent_with_name_and_model(self, paths):
        run_cli("install", paths, model="opus")
        content = (paths["agents"] / "heavy-task-opus.md").read_text()
        assert "name: heavy-task-opus" in content
        assert "model: opus" in content
        assert manifest(paths)["agent_file"].endswith("heavy-task-opus.md")

    def test_model_change_removes_old_agent(self, paths):
        run_cli("install", paths, model="opus")
        run_cli("install", paths, model="fable")
        assert not (paths["agents"] / "heavy-task-opus.md").exists()
        assert (paths["agents"] / "heavy-task-fable.md").exists()

    def test_legacy_unsuffixed_agent_removed(self, paths):
        paths["agents"].mkdir(parents=True)
        legacy = paths["agents"] / "heavy-task.md"
        legacy.write_text("---\nname: heavy-task\nmodel: opus\n---\n")
        run_cli("install", paths, model="opus")
        assert not legacy.exists()
        assert (paths["agents"] / "heavy-task-opus.md").exists()

    def test_reinstall_same_model_idempotent(self, paths):
        run_cli("install", paths, model="opus")
        run_cli("install", paths, model="opus")
        agents = list(paths["agents"].glob("heavy-task*.md"))
        assert len(agents) == 1

    def test_foreign_agent_files_untouched(self, paths):
        paths["agents"].mkdir(parents=True)
        foreign = paths["agents"] / "my-own-agent.md"
        foreign.write_text("mine")
        run_cli("install", paths, model="opus")
        assert foreign.read_text() == "mine"


class TestUninstall:
    def test_removes_generated_agent_and_manifest_entry(self, paths):
        run_cli("install", paths, model="opus")
        run_cli("uninstall", paths)
        assert not list(paths["agents"].glob("heavy-task*.md"))
        assert "agent_file" not in manifest(paths)

    def test_uninstall_without_install_is_safe(self, paths):
        assert run_cli("uninstall", paths) == 0
