import json

import pytest

import complexity_router as router
import status_report


def assistant(model="claude-sonnet-5", msg_id="a"):
    return json.dumps({"type": "assistant", "message": {"id": msg_id, "model": model, "usage": {}}})


def task_call(agent, call_id="t1"):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": "Task", "input": {"subagent_type": agent}},
    ]}})


def task_result(call_id="t1", is_error=False, content="done"):
    return json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": call_id, "is_error": is_error, "content": content},
    ]}})


def write_session(root, name, lines):
    session = root / "project" / f"{name}.jsonl"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text("\n".join(lines) + "\n")
    return session


PRICED = {
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
}
CONFIG = {"models": {"complex": "fable", "simple": "sonnet"}, "pricing_usd_per_mtok": PRICED}


class TestScanTranscripts:
    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        result = status_report.scan_transcripts(tmp_path / "nope")
        assert result == {"sessions": 0, "models": {}, "delegations": {}}

    def test_counts_each_model_once_per_session(self, tmp_path):
        write_session(tmp_path, "one", [assistant("claude-sonnet-5"), assistant("claude-sonnet-5", "b")])
        write_session(tmp_path, "two", [assistant("claude-sonnet-5"), assistant("claude-fable-5", "b")])
        result = status_report.scan_transcripts(tmp_path)
        assert result["sessions"] == 2
        assert result["models"] == {"claude-sonnet-5": 2, "claude-fable-5": 1}

    def test_pairs_a_delegation_with_its_outcome(self, tmp_path):
        write_session(tmp_path, "s", [task_call("heavy-task-fable"), task_result()])
        assert status_report.scan_transcripts(tmp_path)["delegations"] == {
            "heavy-task-fable": {"attempts": 1, "failures": 0, "last_error": None}
        }

    def test_records_a_failure_and_its_reason(self, tmp_path):
        write_session(tmp_path, "s", [
            task_call("heavy-task-fable"), task_result(is_error=True, content="reached your Fable 5 limit"),
        ])
        record = status_report.scan_transcripts(tmp_path)["delegations"]["heavy-task-fable"]
        assert record == {"attempts": 1, "failures": 1, "last_error": "reached your Fable 5 limit"}

    def test_truncates_a_long_error(self, tmp_path):
        write_session(tmp_path, "s", [task_call("heavy-task-fable"), task_result(is_error=True, content="x" * 500)])
        error = status_report.scan_transcripts(tmp_path)["delegations"]["heavy-task-fable"]["last_error"]
        assert len(error) <= status_report.MAX_ERROR_CHARS

    def test_tracks_the_middle_tier_too(self, tmp_path):
        write_session(tmp_path, "s", [task_call("mid-task-sonnet", "m"), task_result("m")])
        assert "mid-task-sonnet" in status_report.scan_transcripts(tmp_path)["delegations"]

    @pytest.mark.parametrize("agent", ["general-purpose", "Explore", "claude-code-guide", ""])
    def test_ignores_agents_that_are_not_ours(self, tmp_path, agent):
        write_session(tmp_path, "s", [task_call(agent), task_result()])
        assert status_report.scan_transcripts(tmp_path)["delegations"] == {}

    def test_a_result_without_a_matching_call_is_ignored(self, tmp_path):
        write_session(tmp_path, "s", [task_result("orphan", is_error=True)])
        assert status_report.scan_transcripts(tmp_path)["delegations"] == {}

    def test_counts_repeated_attempts_across_sessions(self, tmp_path):
        for name in ("a", "b"):
            write_session(tmp_path, name, [task_call("heavy-task-fable"), task_result(is_error=True)])
        record = status_report.scan_transcripts(tmp_path)["delegations"]["heavy-task-fable"]
        assert record["attempts"] == 2 and record["failures"] == 2

    def test_honours_the_session_limit(self, tmp_path):
        for i in range(5):
            write_session(tmp_path, f"s{i}", [assistant()])
        assert status_report.scan_transcripts(tmp_path, limit=2)["sessions"] == 2

    @pytest.mark.parametrize("line", ["{not json", "null", "[]", '"a string"', ""])
    def test_survives_unparseable_lines(self, tmp_path, line):
        write_session(tmp_path, "s", [line, assistant()])
        assert status_report.scan_transcripts(tmp_path)["models"] == {"claude-sonnet-5": 1}

    def test_survives_a_message_whose_content_is_not_a_list(self, tmp_path):
        write_session(tmp_path, "s", [json.dumps({"type": "assistant", "message": {"content": "text"}})])
        assert status_report.scan_transcripts(tmp_path)["sessions"] == 1


class TestTranscriptWarnings:
    def _scan(self, **overrides):
        return {"sessions": 5, "models": {}, "delegations": {}, **overrides}

    def test_nothing_is_claimed_without_any_history(self):
        assert status_report.transcript_warnings(CONFIG, self._scan(sessions=0)) == []

    def test_a_failed_delegation_is_broken(self):
        scan = self._scan(delegations={
            "heavy-task-fable": {"attempts": 3, "failures": 2, "last_error": "no quota"}
        })
        severity, message = status_report.transcript_warnings(CONFIG, scan)[0]
        assert severity == router.BROKEN
        assert "2 of 3" in message and "no quota" in message

    def test_a_working_delegation_is_not_reported(self):
        scan = self._scan(delegations={
            "heavy-task-fable": {"attempts": 3, "failures": 0, "last_error": None}
        })
        assert status_report.transcript_warnings(CONFIG, scan) == []

    def test_routing_on_but_never_used_is_advice_not_a_fault(self):
        findings = status_report.transcript_warnings(CONFIG, self._scan())
        assert [severity for severity, _ in findings] == [router.ADVICE]
        assert "no prompt was delegated" in findings[0][1]

    def test_routing_off_makes_the_absence_expected(self):
        config = {**CONFIG, "routing": {"enabled": False}}
        assert status_report.transcript_warnings(config, self._scan()) == []

    def test_an_unpriced_model_that_generated_is_reported(self):
        scan = self._scan(models={"claude-sonnet-5": 2, "claude-newthing-9": 1},
                          delegations={"heavy-task-fable": {"attempts": 1, "failures": 0, "last_error": None}})
        messages = [message for _, message in status_report.transcript_warnings(CONFIG, scan)]
        assert any("claude-newthing-9" in message for message in messages)
        assert not any("claude-sonnet-5" in message for message in messages)

    def test_placeholder_models_are_not_reported_as_unpriced(self):
        scan = self._scan(models={"<synthetic>": 4},
                          delegations={"heavy-task-fable": {"attempts": 1, "failures": 0, "last_error": None}})
        assert status_report.transcript_warnings(CONFIG, scan) == []


class TestRender:
    """render_checks derives the agents directory from home.parent, so `home` must be nested
    inside this test's own tmp_path — never tmp_path itself, which pytest shares across tests."""

    @pytest.fixture
    def home(self, tmp_path):
        install = tmp_path / "model-switcher"
        install.mkdir()
        return install

    def _lines(self, func, *args):
        out: list[str] = []
        result = func(*args, echo=out.append)
        return "\n".join(out), result

    def test_summary_shows_the_configured_tiers_and_session_model(self, home):
        text, _ = self._lines(status_report.render_summary, CONFIG, home, "opus[1m]")
        assert "opus[1m]" in text and "simple=sonnet" in text and "complex=fable" in text
        assert "routing         enabled" in text

    def test_summary_calls_out_disabled_routing(self, home):
        config = {**CONFIG, "routing": {"enabled": False}}
        text, _ = self._lines(status_report.render_summary, config, home, "sonnet")
        assert "DISABLED" in text

    def test_summary_reports_a_missing_classifier_with_the_fix(self, home):
        text, _ = self._lines(status_report.render_summary, CONFIG, home, "sonnet")
        assert "model-switcher learn" in text

    def test_summary_describes_an_installed_classifier(self, home):
        (home / "classifier.json").write_text(json.dumps({
            "generated_at": "2026-07-25T03:39:15+00:00",
            "corpus": {"sessions": 105, "prompts": 2143},
            "scoring": {"terms": {"alpha": 1.0, "beta": -1.0}},
        }))
        text, _ = self._lines(status_report.render_summary, CONFIG, home, "sonnet")
        assert "2 terms" in text and "2026-07-25" in text and "105 sessions" in text

    def test_summary_reports_unconfigured_pricing_with_the_fix(self, home):
        text, _ = self._lines(status_report.render_summary, {"models": {}}, home, "sonnet")
        assert "model-switcher pricing" in text

    def test_checks_return_the_broken_count_for_an_exit_code(self, home):
        scan = {"sessions": 5, "models": {}, "delegations": {
            "heavy-task-fable": {"attempts": 1, "failures": 1, "last_error": "nope"}}}
        text, broken = self._lines(status_report.render_checks, CONFIG, home, "sonnet", scan)
        assert broken == 1 and "BROKEN" in text

    def test_a_clean_install_says_so_and_returns_zero(self, home):
        # Session on the cheap tier, heavy tier genuinely dearer, routing deliberately off.
        config = {**CONFIG, "routing": {"enabled": False}}
        scan = {"sessions": 3, "models": {}, "delegations": {}}
        text, broken = self._lines(status_report.render_checks, config, home, "sonnet", scan)
        assert broken == 0 and "nothing wrong found" in text

    def test_the_same_model_on_both_tiers_is_flagged(self, home):
        config = {"models": {"complex": "fable", "simple": "fable"}, "pricing_usd_per_mtok": PRICED}
        scan = {"sessions": 3, "models": {}, "delegations": {
            "heavy-task-fable": {"attempts": 1, "failures": 0, "last_error": None}}}
        text, broken = self._lines(status_report.render_checks, config, home, "fable", scan)
        assert broken == 0 and "not a step up" in text

    def test_advice_is_reported_without_making_the_command_fail(self, home):
        scan = {"sessions": 3, "models": {}, "delegations": {}}
        text, broken = self._lines(status_report.render_checks, CONFIG, home, "opus[1m]", scan)
        assert broken == 0 and "note" in text
