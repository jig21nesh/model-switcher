"""Trust-boundary regressions use isolated installs and synthetic data; never a live API."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_router
import complexity_router as router
import jev_router as jev
import merge_settings


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / ".claude" / "model-switcher"
    home.mkdir(parents=True)
    agents = home.parent / "agents"
    agents.mkdir()
    (agents / "heavy-task-fable.md").write_text("synthetic agent")
    project = tmp_path / "repo"
    (project / ".claude").mkdir(parents=True)
    monkeypatch.setenv("MODEL_SWITCHER_HOME", str(home))
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-assessment-credential")
    calls = []

    def transport(body, *_args):
        calls.append(body)
        return {"error": "timeout"}

    monkeypatch.setattr(jev, "call_api", transport)
    config = {
        "models": {"simple": "sonnet", "standard": "opus", "complex": "fable"},
        "routing": {"enabled": True, "agents": True, "show_decisions": True, "show_exchange": True},
        "complexity": {"threshold": 7, "standard_threshold": 3},
        "jev": {"enabled": True, "mode": "shadow", "log_content": True,
                "scope": "projects", "allowed_projects": [str(project.resolve())]},
        "checks": {"in_session": False},
        "pricing_usd_per_mtok": {"sonnet": {"input": 1}},
    }
    return home, project, config, calls


def invoke(setup, override=None, tool=None, prompt="Refactor auth, migrate schema and implement tests end-to-end"):
    home, project, config, _ = setup
    (home / "config.json").write_text(json.dumps(config))
    (project / ".claude/model-switcher.json").write_text(json.dumps(override or {}))
    payload = {"cwd": str(project), "prompt": prompt}
    if tool:
        payload.update(tool_name=tool, tool_input={"subagent_type": "general-purpose", "prompt": prompt,
                                                  "description": "synthetic", "run_in_background": True})
    output = (agent_router.run if tool else router.run)(json.dumps(payload))
    return json.loads(output) if output else {}


def events(home):
    return [json.loads(line) for line in (home / "logs/jev.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("routing", [None, False, "invalid", {"enabled": False}, {"enabled": "yes"}])
@pytest.mark.parametrize("tool", [None, "Task", "Agent"])
def test_repository_cannot_resume_global_opt_out_or_external_calls(setup, routing, tool):
    home, _, config, calls = setup
    config["routing"] = routing
    assert invoke(setup, {"routing": {"enabled": True, "agents": True, "show_decisions": False}}, tool) == {}
    assert not calls and not (home / "logs").exists()


@pytest.mark.parametrize("tool", ["Task", "Agent"])
def test_agent_rewrite_preserves_input_without_approving_it(setup, tool):
    home, _, _, calls = setup
    output = invoke(setup, tool=tool)
    hook = output["hookSpecificOutput"]
    assert "permissionDecision" not in hook and "decision" not in output
    assert hook["updatedInput"]["subagent_type"] == "heavy-task-fable"
    assert hook["updatedInput"]["run_in_background"] is True
    assert hook["updatedInput"]["description"] == "synthetic"
    assert not calls  # offline agent routing still works without delegated-context consent
    assert events(home)[-1]["jev"]["status"] == "agent_not_enabled"


@pytest.mark.parametrize("agents", [False, "yes"])
def test_repository_cannot_resume_agent_routing(setup, agents):
    home, _, config, calls = setup
    config["routing"]["agents"] = agents
    assert invoke(setup, {"routing": {"agents": True}}, "Agent") == {}
    assert not calls and not (home / "logs").exists()


def test_unknown_keys_and_specialist_membership_never_merge(setup):
    _, _, config, _ = setup
    original = copy.deepcopy(config)
    attack = {"routing": {"enabled": True, "generic_agents": ["Explore"], "tiers": 2,
                          "future_permission": True, "show_exchange": True},
              "complexity": {"threshold": 1}, "models": {"complex": "other"},
              "jev": {"enabled": True, "scope": "all", "evaluate_agents": True, "log_content": True}}
    assert router.merge_project_config(config, attack) == original
    assert config == original
    assert "Explore" not in agent_router.generic_agents(router.merge_project_config(config, attack))


def test_repository_opt_outs_work_without_erasing_other_global_fields(setup):
    _, _, config, _ = setup
    merged = router.merge_project_config(config, {"routing": {"show_exchange": False, "agents": False},
                                                "jev": {"enabled": False, "log_content": False}})
    assert merged["jev"]["enabled"] is False and merged["jev"]["scope"] == "projects"
    assert merged["routing"]["agents"] is False
    assert merged["routing"]["show_decisions"] is True


def test_user_owned_tuning_matches_exact_canonical_directory(setup):
    _, project, config, _ = setup
    config["project_settings"] = {str(project.resolve()): {
        "routing": {"tiers": 2, "enabled": True, "generic_agents": ["Explore"]},
        "complexity": {"threshold": 9, "standard_threshold": 4},
        "jev": {"enabled": True, "scope": "all"}}}
    (project / ".claude/model-switcher.json").write_text('{"complexity":{"threshold":1}}')
    resolved = router.resolve_project_config(config, str(project))
    assert resolved["complexity"] == {"threshold": 9, "standard_threshold": 4}
    assert router.tiers_configured(resolved) == 2
    assert resolved["_policy"]["tuning"] == "user_project"
    assert resolved["_policy"]["repository_config_present"] is True
    assert resolved["_policy"]["repository_opt_out"] is False
    assert "generic_agents" not in resolved["routing"] and resolved["jev"] == config["jev"]
    child = project / "child"
    child.mkdir()
    assert router.resolve_project_config(config, str(child))["complexity"] == config["complexity"]
    config["routing"] = {"enabled": False}
    assert not router.routing_enabled(router.resolve_project_config(config, str(project)))


@pytest.mark.parametrize("tuning", [None, [], {"routing": "bad"}, {"complexity": {"threshold": True}},
                                    {"complexity": {"threshold": float("nan")}}, {"routing": {"tiers": 4}}])
def test_invalid_user_tuning_retains_global_policy(setup, tuning):
    _, project, config, _ = setup
    config["project_settings"] = {str(project.resolve()): tuning}
    effective = router.resolve_project_config(config, str(project))
    assert effective["complexity"] == config["complexity"]
    assert effective["_policy"]["tuning"] == "global"


@pytest.mark.parametrize("kind", ["file_symlink", "directory_symlink", "directory", "oversized", "malformed"])
def test_project_reader_rejects_unsafe_or_invalid_files(tmp_path, kind):
    project = tmp_path / "repo"
    directory = project / ".claude"
    directory.mkdir(parents=True)
    path = directory / "model-switcher.json"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "model-switcher.json"
    victim.write_text('{"routing":{"enabled":false}}')
    if kind == "file_symlink":
        path.symlink_to(victim)
    elif kind == "directory_symlink":
        directory.rmdir()
        directory.symlink_to(outside, target_is_directory=True)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("x" * (router.PROJECT_CONFIG_MAX_BYTES + 1) if kind == "oversized" else "{broken")
    assert router.load_project_config(str(project)) == {}


def test_fifo_cannot_stall_project_reader(tmp_path):
    (tmp_path / ".claude").mkdir()
    os.mkfifo(tmp_path / ".claude/model-switcher.json")
    code = "import complexity_router as r, sys; assert r.load_project_config(sys.argv[1]) == {}"
    env = {**os.environ, "PYTHONPATH": str(Path(router.__file__).parent)}
    completed = subprocess.run([sys.executable, "-c", code, str(tmp_path)], env=env,
                               capture_output=True, timeout=3, check=False)
    assert completed.returncode == 0, completed.stderr.decode()


def test_read_is_bounded_even_if_file_grows_after_stat(tmp_path, monkeypatch):
    (tmp_path / ".claude").mkdir()
    path = tmp_path / ".claude/model-switcher.json"
    path.write_text(json.dumps({"padding": "x" * router.PROJECT_CONFIG_MAX_BYTES}))
    mode = path.stat().st_mode
    monkeypatch.setattr(router.os, "fstat", lambda _: SimpleNamespace(st_mode=mode, st_size=1))
    assert router.load_project_config(str(tmp_path)) == {}


@pytest.mark.parametrize("mode", ["shadow", "route"])
@pytest.mark.parametrize("tool", [None, "Agent", "Task"])
def test_project_consent_is_required_before_transport(setup, mode, tool, monkeypatch):
    home, _, config, calls = setup
    config["jev"].update(mode=mode, allowed_projects=[], evaluate_agents=True)
    monkeypatch.setattr(jev, "api_key", lambda *_: pytest.fail("unapproved project read credentials"))
    output = invoke(setup, tool=tool, prompt="secret task content")
    assert "project_not_allowed" in output["systemMessage"]
    assert not calls
    text = (home / "logs/jev.jsonl").read_text()
    assert "secret task content" not in text + output["systemMessage"]
    assert events(home)[-1]["jev"]["transmission"] == "not_sent"


def test_approved_project_and_explicit_agent_consent_reach_transport(setup):
    home, _, config, calls = setup
    invoke(setup)
    assert len(calls) == 1 and events(home)[-1]["jev"]["consent"] == "projects"
    config["jev"]["evaluate_agents"] = True
    invoke(setup, tool="Agent")
    assert len(calls) == 2 and events(home)[-1]["origin"] == "agent"
    assert events(home)[-1]["jev"]["transmission"] == "attempted"


@pytest.mark.parametrize("secret", ["synthetic-assessment-credential", "Bearer synthetic-token",
                                    "sk-abcdefghijklmnopqrst", "tsai_abcdefghijklmnopqrst",
                                    "apikey_123456789012345678901234567890",
                                    "-----BEGIN RSA PRIVATE KEY-----"])
@pytest.mark.parametrize("origin", ["prompt", "agent", "cli"])
def test_sensitive_content_is_neither_sent_nor_logged_or_displayed(setup, secret, origin):
    home, project, config, calls = setup
    config["jev"]["evaluate_agents"] = True
    notices = []
    local = {"score": 3, "tier": "standard", "source": "local", "reason": "local"}
    result = jev.evaluate("Review this synthetic secret: " + secret, config, home, config["models"],
                          local, origin, cwd=str(project), on_result=notices.append,
                          offline={"signals": [secret], "matched_terms": {secret: 1}})
    assert result["reason"] == "sensitive_content" and result["tier"] == "standard"
    assert not calls and len(events(home)) == 1
    text = (home / "logs/jev.jsonl").read_text() + json.dumps(notices)
    assert secret not in text and "Review this" not in text
    assert "request" not in events(home)[0] and "prompt" not in events(home)[0]


@pytest.mark.parametrize("options,origin,reason", [
    ({}, "prompt", "project_not_allowed"),
    ({"scope": "all"}, "agent", "agent_not_enabled"),
    ({"scope": "all", "evaluate_agents": "true"}, "agent", "agent_not_enabled"),
    ({"scope": "other"}, "prompt", "invalid_config"),
    ({"allowed_projects": "all"}, "prompt", "invalid_config"),
    ({"allowed_projects": [1]}, "prompt", "invalid_config"),
    ({"allowed_projects": ["relative"]}, "prompt", "invalid_config"),
    ({"allowed_projects": ["/a"] * 257}, "prompt", "invalid_config"),
    ({"scope": "all"}, "unknown", "invalid_origin"),
    ({"scope": "all"}, "prompt", None),
    ({}, "cli", None),
])
def test_consent_settings_fail_closed(options, origin, reason):
    assert jev.policy_denial(options, origin, None) == reason


def test_project_path_requires_an_existing_directory_and_resolves_aliases(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target)
    file = tmp_path / "file"
    file.touch()
    for value in (None, "", 123, "relative", str(file), str(tmp_path / "missing"), "/bad\x00path"):
        assert jev.project_path(value) is None
    assert jev.project_path(str(alias)) == str(target.resolve())


def test_installer_migrates_only_owned_matcher_and_is_idempotent(tmp_path):
    install_dir = tmp_path / "model-switcher"
    own = {"type": "command", "command": merge_settings.agent_hook_command(install_dir),
           "statusMessage": "Custom progress", "timeout": 4}
    foreign = {"type": "command", "command": "echo foreign"}
    original = {"hooks": {"PreToolUse": [{"matcher": "Task", "hooks": [own, foreign]}]}}
    settings = copy.deepcopy(original)
    manifest = {}
    config = {"routing": {"show_decisions": True}}
    merge_settings.install(settings, manifest, config, install_dir, None)
    first = copy.deepcopy(settings)
    merge_settings.install(settings, manifest, config, install_dir, None)
    assert settings == first
    assert settings["hooks"]["PreToolUse"] == [
        {"matcher": "Task", "hooks": [foreign]},
        {"matcher": "^(Agent|Task)$", "hooks": [own]},
    ]
