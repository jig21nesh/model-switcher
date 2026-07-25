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


MID_SOURCE = Path(__file__).resolve().parent.parent / "agents" / "mid-task.md"


def run_tier_cli(action, paths, model=None, tier=None, source=None):
    argv = [action, "--agents-dir", str(paths["agents"]), "--manifest", str(paths["manifest"])]
    if action == "install":
        argv += ["--source", str(source or MID_SOURCE), "--model", model or "sonnet"]
    if tier:
        argv += ["--tier", tier]
    return generate_agent.main(argv)


class TestStandardTier:
    def test_names_the_middle_agent_with_its_own_prefix(self):
        assert generate_agent.agent_name("sonnet", "standard") == "mid-task-sonnet"

    def test_writes_the_middle_agent_and_records_a_separate_manifest_key(self, paths):
        run_tier_cli("install", paths, tier="standard")
        written = paths["agents"] / "mid-task-sonnet.md"
        assert written.exists()
        assert manifest(paths)["standard_agent_file"] == str(written)
        assert "name: mid-task-sonnet" in written.read_text()
        assert "model: sonnet" in written.read_text()

    def test_both_tiers_coexist(self, paths):
        run_cli("install", paths, model="fable")
        run_tier_cli("install", paths, tier="standard")
        assert (paths["agents"] / "heavy-task-fable.md").exists()
        assert (paths["agents"] / "mid-task-sonnet.md").exists()
        recorded = manifest(paths)
        assert recorded["agent_file"].endswith("heavy-task-fable.md")
        assert recorded["standard_agent_file"].endswith("mid-task-sonnet.md")

    def test_installing_one_tier_leaves_the_other_alone(self, paths):
        run_cli("install", paths, model="fable")
        run_tier_cli("install", paths, tier="standard")
        run_cli("install", paths, model="opus")
        assert (paths["agents"] / "mid-task-sonnet.md").exists()
        assert not (paths["agents"] / "heavy-task-fable.md").exists()

    def test_changing_the_middle_model_replaces_the_old_agent(self, paths):
        run_tier_cli("install", paths, model="sonnet", tier="standard")
        run_tier_cli("install", paths, model="haiku", tier="standard")
        assert (paths["agents"] / "mid-task-haiku.md").exists()
        assert not (paths["agents"] / "mid-task-sonnet.md").exists()

    def test_uninstalling_one_tier_keeps_the_other(self, paths):
        run_cli("install", paths, model="fable")
        run_tier_cli("install", paths, tier="standard")
        run_tier_cli("uninstall", paths, tier="standard")
        assert (paths["agents"] / "heavy-task-fable.md").exists()
        assert not (paths["agents"] / "mid-task-sonnet.md").exists()
        assert "standard_agent_file" not in manifest(paths)
        assert "agent_file" in manifest(paths)

    def test_uninstall_without_a_tier_removes_every_tier(self, paths):
        run_cli("install", paths, model="fable")
        run_tier_cli("install", paths, tier="standard")
        run_cli("uninstall", paths)
        assert not list(paths["agents"].glob("*.md"))
        assert manifest(paths) == {}

    def test_dropping_to_two_tiers_is_safe_when_no_middle_agent_exists(self, paths):
        run_cli("install", paths, model="fable")
        run_tier_cli("uninstall", paths, tier="standard")
        assert (paths["agents"] / "heavy-task-fable.md").exists()

    def test_never_deletes_a_file_it_did_not_generate(self, paths):
        run_tier_cli("install", paths, tier="standard")
        bystander = paths["agents"] / "my-own-agent.md"
        bystander.write_text("mine", encoding="utf-8")
        run_cli("uninstall", paths)
        assert bystander.read_text(encoding="utf-8") == "mine"

    def test_ignores_a_manifest_pointing_outside_the_agents_directory(self, paths, tmp_path):
        outsider = tmp_path / "mid-task-elsewhere.md"
        outsider.write_text("not ours", encoding="utf-8")
        paths["agents"].mkdir(parents=True, exist_ok=True)
        paths["manifest"].write_text(json.dumps({"standard_agent_file": str(outsider)}))
        run_tier_cli("uninstall", paths, tier="standard")
        assert outsider.exists(), "a manifest path outside the agents dir must not be deleted"
