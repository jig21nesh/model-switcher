import json

import pytest

import uninstall

EXISTING_SETTINGS = {"model": "opus", "permissions": {"allow": ["Bash(ls:*)"]}}
EXISTING_CLAUDE_MD = "# My rules\n\nAlways write tests.\n"


@pytest.fixture
def installed(tmp_path):
    """A directory tree shaped like a finished install."""
    claude = tmp_path / ".claude"
    install = claude / "model-switcher"
    agents = claude / "agents"
    for directory in (install / "state", install / "__pycache__", agents):
        directory.mkdir(parents=True)

    (claude / "settings.json").write_text(json.dumps({
        **EXISTING_SETTINGS,
        "statusLine": {"type": "command", "command": f'python3 "{install}/cost_statusline.py"'},
        "hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": f'python3 "{install}/complexity_router.py"'}]}
        ]},
    }))
    (claude / "CLAUDE.md").write_text(
        EXISTING_CLAUDE_MD + "\n<!-- model-switcher:begin -->\npolicy\n<!-- model-switcher:end -->\n"
    )
    (agents / "heavy-task-fable.md").write_text("agent")
    (agents / "mid-task-sonnet.md").write_text("agent")
    (agents / "my-own-agent.md").write_text("mine")

    for name in uninstall.INSTALLED_FILES:
        (install / name).write_text("installed")
    (install / "installed.json").write_text(json.dumps({
        "agent_file": str(agents / "heavy-task-fable.md"),
        "standard_agent_file": str(agents / "mid-task-sonnet.md"),
        "created_claude_md": False,
    }))
    for name in ("config.json", "classifier.json"):
        (install / name).write_text("{}")
    (install / "state" / "abc.json").write_text("{}")
    (install / "__pycache__" / "cli.pyc").write_bytes(b"stale")
    return claude, install


class TestRun:
    def test_restores_settings_exactly(self, installed):
        claude, install = installed
        uninstall.run(claude, install)
        assert json.loads((claude / "settings.json").read_text()) == EXISTING_SETTINGS

    def test_restores_claude_md_exactly(self, installed):
        claude, install = installed
        uninstall.run(claude, install)
        assert (claude / "CLAUDE.md").read_text() == EXISTING_CLAUDE_MD

    def test_removes_every_installed_file(self, installed):
        claude, install = installed
        uninstall.run(claude, install)
        remaining = [name for name in uninstall.INSTALLED_FILES if (install / name).exists()]
        assert remaining == []

    def test_removes_both_tier_agents_but_not_the_users_own(self, installed):
        claude, install = installed
        uninstall.run(claude, install)
        agents = sorted(p.name for p in (claude / "agents").glob("*.md"))
        assert agents == ["my-own-agent.md"]

    def test_removes_state_and_stale_bytecode(self, installed):
        claude, install = installed
        uninstall.run(claude, install)
        assert not (install / "state").exists() and not (install / "__pycache__").exists()

    def test_keeps_user_data_and_reports_it(self, installed):
        claude, install = installed
        kept = uninstall.run(claude, install)
        assert (install / "config.json").exists() and (install / "classifier.json").exists()
        assert any(path.endswith("config.json") for path in kept)
        assert any(path.endswith("classifier.json") for path in kept)

    def test_never_follows_a_symlink_out_of_the_install_directory(self, installed, tmp_path):
        claude, install = installed
        outsider = tmp_path / "precious.py"
        outsider.write_text("not ours")
        (install / "cli.py").unlink()
        (install / "cli.py").symlink_to(outsider)
        uninstall.run(claude, install)
        assert outsider.exists(), "a symlinked path must not be deleted"

    def test_is_safe_to_run_twice(self, installed):
        claude, install = installed
        uninstall.run(claude, install)
        assert uninstall.run(claude, install) == [
            str(install / "config.json"), str(install / "classifier.json")
        ]

    def test_survives_a_missing_manifest(self, installed):
        claude, install = installed
        (install / "installed.json").unlink()
        uninstall.run(claude, install)
        assert not (install / "complexity_router.py").exists()


class TestMain:
    def test_reports_what_it_kept(self, installed, capsys):
        claude, install = installed
        code = uninstall.main(["--claude-dir", str(claude), "--install-dir", str(install)])
        out = capsys.readouterr().out
        assert code == 0 and "model-switcher removed" in out and "config.json" in out

    def test_says_nothing_was_kept_when_nothing_remains(self, tmp_path, capsys):
        claude = tmp_path / ".claude"
        install = claude / "model-switcher"
        install.mkdir(parents=True)
        uninstall.main(["--claude-dir", str(claude), "--install-dir", str(install)])
        assert "Kept:" not in capsys.readouterr().out
