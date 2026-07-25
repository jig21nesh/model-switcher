"""End-to-end install/uninstall against a throwaway CLAUDE_DIR.

Unit tests cover each merge script in isolation; this covers the one thing they cannot —
that install.sh wires the real pieces together and that uninstall puts the user's
settings.json and CLAUDE.md back exactly as they were.
"""

import json
import os
import shutil
import subprocess
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
    assert simple.returncode == 0 and simple.stdout.strip() == ""

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
