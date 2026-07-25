import json

import pytest

import agent_router
import complexity_router as router

COMPLEX_PROMPT = "refactor the auth module, migrate the schema and add tests end-to-end"
SIMPLE_PROMPT = "what does this function do?"
CONFIGURED = {"models": {"complex": "fable", "simple": "sonnet"}}
THREE_TIER = {
    "models": {"complex": "fable", "standard": "sonnet", "simple": "haiku"},
    "complexity": {"threshold": 5, "standard_threshold": 3},
}


@pytest.fixture
def install(tmp_path, monkeypatch):
    """A tree shaped like a real install, so claude_dir() stays inside this test."""
    claude = tmp_path / ".claude"
    home = claude / "model-switcher"
    home.mkdir(parents=True)
    (claude / "agents").mkdir()
    monkeypatch.setenv("MODEL_SWITCHER_HOME", str(home))
    return claude, home


def configure(home, config):
    (home / "config.json").write_text(json.dumps(config))


def make_agents(claude, *names):
    for name in names:
        (claude / "agents" / f"{name}.md").write_text("agent")


def hook_input(subagent_type="general-purpose", prompt=COMPLEX_PROMPT, **extra):
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt, "description": "do it"},
        **extra,
    }
    return json.dumps(payload)


def rewritten(output):
    """The subagent_type the hook asked for, or None when it left the call alone."""
    if not output:
        return None
    return json.loads(output)["hookSpecificOutput"]["updatedInput"]["subagent_type"]


class TestUpgrading:
    def test_a_generic_agent_given_complex_work_is_moved_to_the_heavy_tier(self, install):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        assert rewritten(agent_router.run(hook_input())) == "heavy-task-fable"

    def test_the_middle_tier_is_used_when_the_work_only_warrants_it(self, install):
        claude, home = install
        configure(home, THREE_TIER)
        make_agents(claude, "heavy-task-fable", "mid-task-sonnet")
        out = agent_router.run(hook_input(prompt="fix the failing test in the config parser"))
        assert rewritten(out) == "mid-task-sonnet"

    def test_simple_work_is_left_on_the_generic_agent(self, install):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        assert agent_router.run(hook_input(prompt=SIMPLE_PROMPT)) == ""

    def test_the_rest_of_the_tool_input_is_preserved(self, install):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        updated = json.loads(agent_router.run(hook_input()))["hookSpecificOutput"]["updatedInput"]
        assert updated["prompt"] == COMPLEX_PROMPT and updated["description"] == "do it"

    def test_the_decision_is_explained_rather_than_silent(self, install):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        block = json.loads(agent_router.run(hook_input()))["hookSpecificOutput"]
        assert block["permissionDecision"] == "allow"
        assert "heavy-task-fable" in block["additionalContext"]
        assert '"agents": false' in block["additionalContext"]


class TestLeavingWellAlone:
    def test_an_explicitly_chosen_agent_is_never_overridden(self, install):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable", "code-reviewer")
        assert agent_router.run(hook_input(subagent_type="code-reviewer")) == ""

    def test_a_cheap_search_agent_is_not_promoted(self, install):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        assert agent_router.run(hook_input(subagent_type="Explore")) == ""

    def test_a_tier_agent_is_never_downgraded(self, install):
        claude, home = install
        configure(home, THREE_TIER)
        make_agents(claude, "heavy-task-fable", "mid-task-sonnet")
        assert agent_router.run(hook_input(subagent_type="heavy-task-fable", prompt=SIMPLE_PROMPT)) == ""

    def test_it_will_not_rewrite_to_an_agent_that_does_not_exist(self, install):
        _, home = install
        configure(home, CONFIGURED)  # no agent files written
        assert agent_router.run(hook_input()) == ""

    def test_it_does_not_promote_agents_spawned_inside_an_agent(self, install):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        assert agent_router.run(hook_input(agent_id="ab12cd34")) == ""

    def test_other_tools_are_ignored(self, install):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        payload = json.loads(hook_input())
        payload["tool_name"] = "Bash"
        assert agent_router.run(json.dumps(payload)) == ""

    def test_it_is_off_when_agent_routing_is_disabled(self, install):
        claude, home = install
        configure(home, {**CONFIGURED, "routing": {"agents": False}})
        make_agents(claude, "heavy-task-fable")
        assert agent_router.run(hook_input()) == ""

    def test_it_is_off_when_routing_as_a_whole_is_disabled(self, install):
        claude, home = install
        configure(home, {**CONFIGURED, "routing": {"enabled": False}})
        make_agents(claude, "heavy-task-fable")
        assert agent_router.run(hook_input()) == ""

    def test_it_is_off_when_models_are_not_configured(self, install):
        claude, home = install
        configure(home, {"models": {}})
        make_agents(claude, "heavy-task-fable")
        assert agent_router.run(hook_input()) == ""

    def test_the_generic_list_can_be_configured(self, install):
        claude, home = install
        configure(home, {**CONFIGURED, "routing": {"generic_agents": ["my-agent"]}})
        make_agents(claude, "heavy-task-fable")
        assert agent_router.run(hook_input(subagent_type="general-purpose")) == ""
        assert rewritten(agent_router.run(hook_input(subagent_type="my-agent"))) == "heavy-task-fable"


class TestHostileInput:
    @pytest.mark.parametrize("payload", [
        "", "not json", "null", "[]", "123", '{"tool_name": "Task"}',
        '{"tool_name": "Task", "tool_input": "text"}',
        '{"tool_name": "Task", "tool_input": {}}',
        '{"tool_name": "Task", "tool_input": {"subagent_type": 5, "prompt": "x"}}',
        '{"tool_name": "Task", "tool_input": {"subagent_type": "general-purpose", "prompt": ""}}',
        '{"tool_name": "Task", "tool_input": {"subagent_type": "general-purpose", "prompt": 5}}',
    ])
    def test_anything_malformed_leaves_the_call_alone(self, install, payload):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        assert agent_router.run(payload) == ""

    def test_a_model_name_that_is_not_one_is_refused(self, install):
        claude, home = install
        configure(home, {"models": {"complex": "fable; rm -rf /", "simple": "sonnet"}})
        make_agents(claude, "heavy-task-fable")
        assert agent_router.run(hook_input()) == ""

    def test_an_enormous_prompt_is_still_handled(self, install):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        huge = (COMPLEX_PROMPT + " ") * 20_000
        assert rewritten(agent_router.run(hook_input(prompt=huge))) == "heavy-task-fable"

    def test_a_corrupt_config_leaves_the_call_alone(self, install):
        claude, home = install
        (home / "config.json").write_text("{not json")
        make_agents(claude, "heavy-task-fable")
        assert agent_router.run(hook_input()) == ""


class TestMain:
    def test_prints_nothing_and_succeeds_when_there_is_nothing_to_do(self, install, monkeypatch, capsys):
        _, home = install
        configure(home, CONFIGURED)
        monkeypatch.setattr("sys.stdin", type("S", (), {"read": staticmethod(lambda: "")})())
        assert agent_router.main() == 0 and capsys.readouterr().out == ""

    def test_never_fails_the_tool_call_even_when_it_crashes(self, install, monkeypatch, capsys):
        def explode():
            raise RuntimeError("boom")
        monkeypatch.setattr("sys.stdin", type("S", (), {"read": staticmethod(explode)})())
        assert agent_router.main() == 0
        assert capsys.readouterr().out == ""

    def test_emits_the_rewrite_on_stdout(self, install, monkeypatch, capsys):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        payload = hook_input()
        monkeypatch.setattr("sys.stdin", type("S", (), {"read": staticmethod(lambda: payload)})())
        assert agent_router.main() == 0
        assert "heavy-task-fable" in capsys.readouterr().out


class TestSharedWithThePromptRouter:
    def test_it_names_the_same_agent_the_prompt_router_would(self, install):
        claude, home = install
        configure(home, CONFIGURED)
        make_agents(claude, "heavy-task-fable")
        config = router.load_config()
        score = router.score_prompt(COMPLEX_PROMPT, {})
        tier = router.select_tier(score, config)
        expected = router.agent_name_for(config["models"][tier], tier)
        assert rewritten(agent_router.run(hook_input())) == expected
