"""End-to-end install/uninstall against a throwaway CLAUDE_DIR.

Unit tests cover each merge script in isolation; this covers the one thing they cannot —
that install.sh wires the real pieces together and that uninstall puts the user's
settings.json and CLAUDE.md back exactly as they were.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"

pytestmark = [
    pytest.mark.lifecycle,
    pytest.mark.skipif(shutil.which("bash") is None, reason="install.sh needs bash"),
]

EXISTING_SETTINGS = {
    "model": "opus",
    "statusLine": {"type": "command", "command": "echo my-own-statusline"},
    "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo unrelated-hook"}]}]},
    "permissions": {"allow": ["Bash(ls:*)"]},
}
EXISTING_CLAUDE_MD = "# My rules\n\nAlways write tests.\n"


def run_installer(claude_dir: Path, *args: str) -> subprocess.CompletedProcess:
    # CLAUDE_DIR is the only thing standing between this test and the developer's real
    # ~/.claude, so it is passed explicitly rather than inherited.
    assert claude_dir != Path.home() / ".claude"
    result = subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env={"CLAUDE_DIR": str(claude_dir), "PATH": os.environ["PATH"], "HOME": str(claude_dir.parent)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"installer failed: {result.stdout}\n{result.stderr}"
    return result


@pytest.fixture
def claude_dir(tmp_path):
    target = tmp_path / "claude-home" / ".claude"
    target.mkdir(parents=True)
    return target


def test_install_then_uninstall_restores_a_preexisting_setup(claude_dir):
    settings = claude_dir / "settings.json"
    claude_md = claude_dir / "CLAUDE.md"
    settings.write_text(json.dumps(EXISTING_SETTINGS, indent=2) + "\n", encoding="utf-8")
    claude_md.write_text(EXISTING_CLAUDE_MD, encoding="utf-8")

    install_out = run_installer(claude_dir, "--skip-model").stdout
    assert "model-switcher installed" in install_out

    installed = json.loads(settings.read_text(encoding="utf-8"))
    hook_commands = [
        hook["command"]
        for matcher in installed["hooks"]["UserPromptSubmit"]
        for hook in matcher["hooks"]
    ]
    assert any("complexity_router.py" in command for command in hook_commands)
    assert "echo unrelated-hook" in hook_commands, "an unrelated hook must survive install"
    assert "cost_statusline.py" in installed["statusLine"]["command"]
    assert installed["permissions"] == EXISTING_SETTINGS["permissions"]
    assert installed["model"] == "opus", "--skip-model must leave the session model alone"

    assert "model-switcher:begin" in claude_md.read_text(encoding="utf-8")
    assert EXISTING_CLAUDE_MD.strip() in claude_md.read_text(encoding="utf-8")
    assert (claude_dir / "model-switcher" / "config.json").exists()
    assert list((claude_dir / "agents").glob("heavy-task-*.md"))

    run_installer(claude_dir, "--uninstall")

    assert json.loads(settings.read_text(encoding="utf-8")) == EXISTING_SETTINGS
    assert claude_md.read_text(encoding="utf-8") == EXISTING_CLAUDE_MD
    assert not list((claude_dir / "agents").glob("heavy-task-*.md"))
    assert (claude_dir / "model-switcher" / "config.json").exists(), "config must survive uninstall"


def test_upgrade_routes_both_agent_names_without_permissions_and_keeps_opt_out(claude_dir, tmp_path):
    settings_path = claude_dir / "settings.json"
    original = json.dumps(EXISTING_SETTINGS, indent=4)
    settings_path.write_text(original)
    run_installer(claude_dir, "--skip-model")
    settings = json.loads(settings_path.read_text())
    owned = settings["hooks"]["PreToolUse"][0]
    owned["matcher"] = "Task"  # simulate the previous release's matcher
    foreign = {"matcher": "Task", "hooks": [{"type": "command", "command": "echo unrelated"}]}
    settings["hooks"]["PreToolUse"].append(foreign)
    settings_path.write_text(json.dumps(settings))
    home = claude_dir / "model-switcher"
    config = json.loads((home / "config.json").read_text())
    config["jev"]["enabled"] = True  # no scope approval: must remain offline
    (home / "config.json").write_text(json.dumps(config))
    run_installer(claude_dir, "--skip-model")
    updated = json.loads(settings_path.read_text())["hooks"]["PreToolUse"]
    assert foreign in updated
    assert any(m.get("matcher") == "^(Agent|Task)$" for m in updated)
    assert json.loads((home / "config.json").read_text())["jev"] == config["jev"]

    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    override = project / ".claude/model-switcher.json"
    override.write_text('{"routing":{"enabled":true},"jev":{"enabled":true,"scope":"all"}}')
    env = {"MODEL_SWITCHER_HOME": str(home), "PATH": os.environ["PATH"],
           "PYTHONPATH": str(home)}
    # Stub the installed module's transport so a regression cannot send a real request.
    code = ("import agent_router as a, jev_router as j; "
            "j.call_api=lambda *args: (_ for _ in ()).throw(AssertionError('unexpected network')); "
            "raise SystemExit(a.main())")
    for tool in ("Agent", "Task"):
        payload = {"tool_name": tool, "cwd": str(project), "tool_input": {
            "subagent_type": "general-purpose",
            "prompt": "refactor auth, migrate schema, implement end-to-end tests"}}
        result = subprocess.run([sys.executable, "-c", code], input=json.dumps(payload), env=env,
                                text=True, capture_output=True, timeout=10, check=True)
        output = json.loads(result.stdout)
        assert "agent_not_enabled" in output["systemMessage"]
        block = output["hookSpecificOutput"]
        assert "permissionDecision" not in block
        assert block["updatedInput"]["subagent_type"] == "heavy-task-fable"

    config["routing"]["enabled"] = False
    (home / "config.json").write_text(json.dumps(config))
    result = subprocess.run([sys.executable, str(home / "complexity_router.py")],
                            input=json.dumps({"cwd": str(project), "prompt": "refactor everything"}),
                            env=env, text=True, capture_output=True, timeout=10, check=True)
    assert result.stdout == ""
    run_installer(claude_dir, "--uninstall")
    # The foreign hook added after first installation survives; permissions are unchanged.
    restored = json.loads(settings_path.read_text())
    assert restored["permissions"] == EXISTING_SETTINGS["permissions"]
    assert restored["hooks"]["PreToolUse"] == [foreign]


def test_uninstall_restores_nonstandard_formatting_byte_for_byte(claude_dir):
    # Neither file is in the shape our writers produce: 4-space indent, no trailing newline.
    settings = claude_dir / "settings.json"
    claude_md = claude_dir / "CLAUDE.md"
    original_settings = '{\n    "model": "opus",\n    "permissions": {"allow": []}\n}'
    original_md = "# My rules\n- no trailing newline"
    settings.write_text(original_settings, encoding="utf-8")
    claude_md.write_text(original_md, encoding="utf-8")

    run_installer(claude_dir, "--skip-model")
    run_installer(claude_dir, "--uninstall")

    assert settings.read_text(encoding="utf-8") == original_settings
    assert claude_md.read_text(encoding="utf-8") == original_md


def test_user_directories_beside_the_install_cannot_shadow_the_cli(claude_dir):
    # ~/.claude/hooks is a real directory on many machines; a same-named module there must
    # never be imported in place of the installed one.
    run_installer(claude_dir, "--skip-model")
    install_dir = claude_dir / "model-switcher"
    (claude_dir / "hooks").mkdir(exist_ok=True)
    for name in ("cli.py", "complexity_router.py"):
        (claude_dir / "hooks" / name).write_text("raise SystemExit('hijacked')\n", encoding="utf-8")

    env = {"PATH": os.environ["PATH"], "HOME": str(claude_dir.parent), "MODEL_SWITCHER_HOME": str(install_dir)}
    result = subprocess.run(
        ["python3", str(install_dir / "model-switcher"), "tiers"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "hijacked" not in result.stdout + result.stderr


def test_install_sets_and_restores_the_session_model(claude_dir):
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")

    run_installer(claude_dir)
    assert json.loads(settings.read_text(encoding="utf-8"))["model"] == "sonnet"

    run_installer(claude_dir, "--uninstall")
    assert json.loads(settings.read_text(encoding="utf-8"))["model"] == "opus"


def test_installed_hook_and_statusline_run_as_claude_code_invokes_them(claude_dir):
    run_installer(claude_dir, "--skip-model")
    install_dir = claude_dir / "model-switcher"
    env = {**os.environ, "MODEL_SWITCHER_HOME": str(install_dir)}

    complex_prompt = json.dumps(
        {"prompt": "refactor the auth module, migrate the schema and add tests", "session_id": "lifecycle"}
    )
    routed = subprocess.run(
        ["python3", str(install_dir / "complexity_router.py")],
        input=complex_prompt, capture_output=True, text=True, env=env, timeout=30,
    )
    assert routed.returncode == 0
    directive = json.loads(routed.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "classified COMPLEX" in directive

    simple = subprocess.run(
        ["python3", str(install_dir / "complexity_router.py")],
        input=json.dumps({"prompt": "what does this function do?", "session_id": "lifecycle"}),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert simple.returncode == 0
    notice = json.loads(simple.stdout)
    assert 'hookSpecificOutput' not in notice
    assert 'Offline: sonnet' in notice['systemMessage'] and 'Jev: disabled' in notice['systemMessage']

    display = subprocess.run(
        [str(install_dir / 'model-switcher'), 'display', '--no-details'],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert display.returncode == 0 and 'display: off' in display.stdout
    assert json.loads((install_dir / 'config.json').read_text())['routing']['show_exchange'] is False

    statusline = subprocess.run(
        ["python3", str(install_dir / "cost_statusline.py")],
        input=json.dumps({"model": {"display_name": "Sonnet 5"}}),
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert statusline.returncode == 0 and statusline.stdout.strip()


def test_uninstall_without_install_is_safe(claude_dir):
    run_installer(claude_dir, "--uninstall")
    assert not (claude_dir / "settings.json").exists() or json.loads(
        (claude_dir / "settings.json").read_text(encoding="utf-8")
    ) == {}


def test_install_generates_both_agents_when_a_middle_tier_is_configured(claude_dir):
    install_dir = claude_dir / "model-switcher"
    install_dir.mkdir(parents=True)
    (install_dir / "config.json").write_text(
        json.dumps({"models": {"complex": "fable", "standard": "sonnet", "simple": "haiku"}}),
        encoding="utf-8",
    )

    output = run_installer(claude_dir, "--skip-model").stdout
    assert "3 tiers" in output
    assert (claude_dir / "agents" / "heavy-task-fable.md").exists()
    assert (claude_dir / "agents" / "mid-task-sonnet.md").exists()

    run_installer(claude_dir, "--uninstall")
    assert not list((claude_dir / "agents").glob("*.md"))


def test_removing_the_middle_model_drops_its_agent_on_reinstall(claude_dir):
    install_dir = claude_dir / "model-switcher"
    install_dir.mkdir(parents=True)
    config = install_dir / "config.json"
    config.write_text(
        json.dumps({"models": {"complex": "fable", "standard": "sonnet", "simple": "haiku"}}),
        encoding="utf-8",
    )
    run_installer(claude_dir, "--skip-model")
    assert (claude_dir / "agents" / "mid-task-sonnet.md").exists()

    config.write_text(json.dumps({"models": {"complex": "fable", "simple": "haiku"}}), encoding="utf-8")
    output = run_installer(claude_dir, "--skip-model").stdout
    assert "2 tiers" in output
    assert not (claude_dir / "agents" / "mid-task-sonnet.md").exists()
    assert (claude_dir / "agents" / "heavy-task-fable.md").exists()


def test_the_cli_is_installed_and_runs_without_the_repo(claude_dir, tmp_path):
    """The maintenance commands must survive the clone being deleted."""
    run_installer(claude_dir, "--skip-model")
    install_dir = claude_dir / "model-switcher"
    cli = install_dir / "model-switcher"
    assert cli.exists() and os.access(cli, os.X_OK)
    for name in ("cli.py", "analyze_history.py", "update_pricing.py", "tune_threshold.py",
                 "classifier_report.py", "decision_boundary.py", "pricing.json"):
        assert (install_dir / name).exists(), f"{name} must be installed for the CLI to work"

    # Copy the install somewhere unrelated and run it with the repo nowhere in sight.
    isolated = tmp_path / "isolated"
    shutil.copytree(install_dir, isolated)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "MODEL_SWITCHER_HOME": str(isolated),
    }
    result = subprocess.run(
        ["python3", str(isolated / "model-switcher"), "explain", "refactor the auth module"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "heavy-task" in result.stdout

    priced = subprocess.run(
        ["python3", str(isolated / "model-switcher"), "pricing", "--offline"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60,
    )
    assert priced.returncode in (0, 1), priced.stderr
    assert "pricing" in priced.stdout

    # A fresh install has learned nothing yet: the report has to say so rather than fail.
    inspected = subprocess.run(
        ["python3", str(isolated / "model-switcher"), "classifier",
         "--transcripts", str(tmp_path / "no-transcripts")],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60,
    )
    assert inspected.returncode == 2
    assert "learn --apply" in inspected.stderr


def test_uninstall_removes_the_cli_and_stale_bytecode(claude_dir):
    run_installer(claude_dir, "--skip-model")
    install_dir = claude_dir / "model-switcher"
    (install_dir / "__pycache__").mkdir(exist_ok=True)
    (install_dir / "__pycache__" / "cli.cpython-312.pyc").write_bytes(b"stale")

    run_installer(claude_dir, "--uninstall")
    assert not (install_dir / "model-switcher").exists()
    assert not (install_dir / "analyze_history.py").exists()
    assert not (install_dir / "pricing.json").exists()
    assert not (install_dir / "__pycache__").exists()
    assert (install_dir / "config.json").exists(), "user config must survive uninstall"


def test_a_learned_classifier_survives_uninstall(claude_dir):
    run_installer(claude_dir, "--skip-model")
    classifier = claude_dir / "model-switcher" / "classifier.json"
    classifier.write_text('{"schema_version": 1, "scoring": {"terms": {"refactor": 0.5}}}')
    run_installer(claude_dir, "--uninstall")
    assert classifier.exists(), "learned weights are user data, like config.json"


def test_uninstall_works_from_the_install_with_no_repo(claude_dir, tmp_path):
    """Someone who deletes the clone must still be able to remove the tool."""
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps(EXISTING_SETTINGS, indent=2) + "\n", encoding="utf-8")
    run_installer(claude_dir, "--skip-model")

    install_dir = claude_dir / "model-switcher"
    (install_dir / "classifier.json").write_text('{"schema_version": 1}', encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "MODEL_SWITCHER_HOME": str(install_dir)}

    dry = subprocess.run(
        ["python3", str(install_dir / "model-switcher"), "uninstall"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60,
    )
    assert dry.returncode == 1 and json.loads(settings.read_text())["hooks"]

    done = subprocess.run(
        ["python3", str(install_dir / "model-switcher"), "uninstall", "--yes"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(settings.read_text(encoding="utf-8")) == EXISTING_SETTINGS
    assert not list((claude_dir / "agents").glob("*.md"))
    assert not (install_dir / "model-switcher").exists()
    assert (install_dir / "config.json").exists() and (install_dir / "classifier.json").exists()


def test_jev_installs_standalone_and_keeps_private_user_data(claude_dir, tmp_path):
    run_installer(claude_dir)
    install_dir = claude_dir / "model-switcher"
    assert (install_dir / "jev_router.py").exists()
    assert "model: opus" in (claude_dir / "agents" / "mid-task-opus.md").read_text()
    assert "model: fable" in (claude_dir / "agents" / "heavy-task-fable.md").read_text()
    isolated = tmp_path / "isolated-jev"
    shutil.copytree(install_dir, isolated)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "MODEL_SWITCHER_HOME": str(isolated)}
    result = subprocess.run(
        ["python3", str(isolated / "model-switcher"), "jev", "--offline"],
        input="What does this do?", capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"]["available_models"] == {
        "simple": "sonnet", "standard": "opus", "complex": "fable",
    }
    missing = subprocess.run(
        ["python3", str(isolated / "model-switcher"), "jev"],
        input="What does this do?", capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=10,
    )
    assert missing.returncode == 1 and 'missing_api_key' in missing.stdout
    (install_dir / "jev-api-key").write_text("test-only-key")
    (install_dir / "jev-api-key").chmod(0o600)
    (install_dir / "logs").mkdir()
    (install_dir / "logs" / "jev.jsonl").write_text('{"event":"test-only"}\n')
    run_installer(claude_dir, "--uninstall")
    assert not (install_dir / "jev_router.py").exists()
    assert (install_dir / "jev-api-key").read_text() == "test-only-key"
    assert (install_dir / "logs" / "jev.jsonl").exists()
