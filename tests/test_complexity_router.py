import json

import pytest

import complexity_router as router

CONFIGURED = {
    "models": {"complex": "opus", "simple": "sonnet"},
    "complexity": {"threshold": 5},
    "pricing_usd_per_mtok": {
        "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3}
    },
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_SWITCHER_HOME", str(tmp_path))
    return tmp_path


def write_config(home, config):
    (home / "config.json").write_text(json.dumps(config))


def hook_input(prompt, session_id="abc-123"):
    return json.dumps({"prompt": prompt, "session_id": session_id, "hook_event_name": "UserPromptSubmit"})


class TestScorePrompt:
    def test_short_question_scores_simple(self):
        assert router.score_prompt("what does this function do?") <= 2

    def test_typo_fix_scores_simple(self):
        assert router.score_prompt("fix the typo in the header") < 5

    def test_strong_keyword_with_moderate_scores_complex(self):
        assert router.score_prompt("refactor the auth module to use middleware and add tests") >= 5

    def test_multi_step_build_request_scores_complex(self):
        prompt = (
            "Build a REST API service with the following:\n"
            "1. user registration endpoint\n2. database schema\n3. security tests"
        )
        assert router.score_prompt(prompt) >= 8

    def test_short_question_with_strong_keyword_not_capped(self):
        assert router.score_prompt("can you refactor and optimize this entire module?") >= 5

    def test_empty_prompt_scores_zero(self):
        assert router.score_prompt("") == 0

    def test_score_clamped_to_ten(self):
        prompt = "refactor migrate implement deploy audit " * 50 + "\n1. a\n2. b\n```code```"
        assert router.score_prompt(prompt) == 10

    def test_multiple_file_paths_add_signal(self):
        with_paths = "update app.py and tests/test_app.py accordingly"
        without = "update the app and the tests accordingly"
        assert router.score_prompt(with_paths) > router.score_prompt(without)

    def test_huge_prompt_handled(self):
        assert 0 <= router.score_prompt("word " * 200_000) <= 10


class TestRun:
    def test_complex_prompt_emits_delegation_directive(self, home):
        write_config(home, CONFIGURED)
        out = json.loads(router.run(hook_input("refactor the auth module, migrate the schema and add tests")))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "heavy-task" in context
        assert "opus" in context
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    def test_simple_prompt_emits_nothing(self, home):
        write_config(home, CONFIGURED)
        assert router.run(hook_input("what does this function do?")) == ""

    def test_unconfigured_models_asks_for_confirmation(self, home):
        write_config(home, {"models": {"complex": None, "simple": "sonnet"}})
        context = json.loads(router.run(hook_input("hello")))["hookSpecificOutput"]["additionalContext"]
        assert "not configured" in context
        assert "config.json" in context

    def test_missing_config_asks_for_both(self, home):
        context = json.loads(router.run(hook_input("hello")))["hookSpecificOutput"]["additionalContext"]
        assert "Model routing is not configured" in context
        assert router.PRICING_URL in context

    def test_unconfigured_pricing_includes_link(self, home):
        write_config(home, {"models": {"complex": "opus", "simple": "sonnet"}})
        context = json.loads(router.run(hook_input("hello")))["hookSpecificOutput"]["additionalContext"]
        assert router.PRICING_URL in context

    def test_nags_only_once_per_session(self, home):
        first = router.run(hook_input("hello", session_id="sess-1"))
        second = router.run(hook_input("hello again", session_id="sess-1"))
        assert first != ""
        assert second == ""

    def test_nag_repeats_for_new_session(self, home):
        router.run(hook_input("hello", session_id="sess-1"))
        assert router.run(hook_input("hello", session_id="sess-2")) != ""

    def test_malformed_stdin_passes_through(self, home):
        assert router.run("this is not json {") == ""

    def test_non_object_stdin_passes_through(self, home):
        assert router.run('["a", "b"]') == ""

    def test_missing_prompt_passes_through(self, home):
        assert router.run(json.dumps({"session_id": "x"})) == ""

    def test_hostile_prompt_stays_data(self, home):
        write_config(home, CONFIGURED)
        hostile = 'refactor this; rm -rf ~ && echo "`cat /etc/passwd`" \x00 {"json": "break"}'
        out = router.run(hook_input(hostile + " and migrate the database schema across the entire codebase"))
        parsed = json.loads(out)
        assert "additionalContext" in parsed["hookSpecificOutput"]

    def test_path_traversal_session_id_writes_nothing_outside_state(self, home):
        write_config(home, {})
        router.run(hook_input("hello", session_id="../../evil"))
        assert not (home.parent / "evil.json").exists()
        assert router._state_path("../../evil") is None

    def test_custom_threshold_respected(self, home):
        config = dict(CONFIGURED)
        config["complexity"] = {"threshold": 10}
        write_config(home, config)
        assert router.run(hook_input("refactor the auth module and add tests")) == ""

    def test_invalid_threshold_falls_back_to_default(self, home):
        config = dict(CONFIGURED)
        config["complexity"] = {"threshold": "high"}
        write_config(home, config)
        out = router.run(hook_input("refactor the auth module, migrate the schema and add tests"))
        assert "heavy-task" in out

    def test_stale_state_files_cleaned_up(self, home):
        state_dir = home / "state"
        state_dir.mkdir(parents=True)
        stale = state_dir / "old-session.json"
        stale.write_text("{}")
        import os
        import time

        os.utime(stale, (time.time() - 8 * 24 * 3600, time.time() - 8 * 24 * 3600))
        router.run(hook_input("hello", session_id="fresh-session"))
        assert not stale.exists()

    def test_main_never_raises_on_garbage(self, home, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
        assert router.main() == 0
        assert capsys.readouterr().out == ""
