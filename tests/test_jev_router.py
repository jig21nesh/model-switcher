import io
import json
import os
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

import agent_router
import cli
import complexity_router as router
import jev_router as jev

CANDIDATES = {"simple": "sonnet", "standard": "opus", "complex": "fable"}
CONFIG = {
    "models": CANDIDATES, "complexity": {"threshold": 7, "standard_threshold": 3},
    "jev": {"enabled": True, "scope": "all", "evaluate_agents": True, "mode": "route", "log_content": True},
    "checks": {"in_session": False},
    "pricing_usd_per_mtok": {"sonnet": {"input": 3, "output": 15}},
}
LOCAL = {"score": 4, "tier": "standard", "source": "local", "reason": "local"}


def response(choice="complex", confidence=0.9):
    return {"model": jev.MODEL, "answers": {"routing_tier": {
        "type": "choice", "choice": choice, "confidence": confidence,
        "probabilities": {tier: 0.9 if tier == choice else 0.05 for tier in CANDIDATES},
    }}, "usage": {"input_tokens": 300, "output_tokens": 30}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "claude" / "model-switcher"
    path.mkdir(parents=True)
    monkeypatch.setenv("MODEL_SWITCHER_HOME", str(path))
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-never-log-this")
    monkeypatch.setattr(jev, "call_api", lambda *args: {"response": response(), "http_status": 200})
    return path


def logs(home):
    return [json.loads(line) for line in (home / "logs" / "jev.jsonl").read_text().splitlines()]


def evaluate(home, config=None, prompt="fix this bug"):
    return jev.evaluate(prompt, config or CONFIG, home, CANDIDATES, LOCAL, "prompt")


@pytest.mark.parametrize("choice", CANDIDATES)
def test_each_tier_is_accepted_and_traced(home, monkeypatch, choice):
    monkeypatch.setattr(jev, "call_api", lambda *a: {"response": response(choice), "http_status": 200})
    result = evaluate(home)
    assert result["tier"] == (None if choice == "simple" else choice)
    assert result["source"] == "jev"
    request, reply = logs(home)
    assert request["request_id"] == reply["request_id"]
    assert request["request"]["state"]["prompt"] == "fix this bug"
    assert request["request"]["state"]["available_models"] == CANDIDATES
    assert set(request["request"]["questions"]["routing_tier"]["criteria"]) == set(CANDIDATES)
    assert reply["response"] == response(choice)
    assert reply["decision"] == result and reply["latency_ms"] >= 0
    assert (home / "logs" / "jev.jsonl").stat().st_mode & 0o777 == 0o600
    assert (home / "logs").stat().st_mode & 0o777 == 0o700


def test_shadow_and_low_confidence_keep_local_route(home, monkeypatch):
    result = evaluate(home, {**CONFIG, "jev": {**CONFIG["jev"], "mode": "shadow"}})
    assert result["tier"] == "standard" and result["reason"] == "shadow"
    monkeypatch.setattr(jev, "call_api", lambda *a: {"response": response("simple", 0.2)})
    result = evaluate(home)
    assert result["tier"] == "standard" and result["reason"] == "low_confidence"
    assert result["jev"]["choice"] == "simple"


@pytest.mark.parametrize("value", [None, False, [], {}, {"enabled": "true"}, {"enabled": 1}])
def test_disabled_is_entirely_offline(home, monkeypatch, value):
    def forbidden(*args):
        pytest.fail("Disabled evaluator touched network or credentials")
    monkeypatch.setattr(jev, "api_key", forbidden)
    monkeypatch.setattr(jev, "call_api", forbidden)
    assert evaluate(home, {**CONFIG, "jev": value}) == LOCAL
    entry = logs(home)[0]
    assert entry["event"] == "routing.decision"
    assert entry["jev"]["status"] == "disabled"
    assert entry["offline"]["tier"] == "standard"
    assert "prompt" not in entry


@pytest.mark.parametrize("options", [
    {"mode": "oops"}, {"mode": []}, {"model": None}, {"model": "jev-foo\n"},
    {"timeout_seconds": True}, {"timeout_seconds": 0}, {"timeout_seconds": 11},
    {"timeout_seconds": float("inf")}, {"min_confidence": float("nan")},
    {"min_confidence": -1}, {"min_confidence": 1.1},
])
def test_invalid_settings_fall_back_without_network(home, monkeypatch, options):
    monkeypatch.setattr(jev, "call_api", lambda *a: pytest.fail("invalid settings reached API"))
    result = evaluate(home, {**CONFIG, "jev": {"enabled": True, "scope": "all", "evaluate_agents": True, **options}})
    assert result["reason"] == "invalid_config" and result["source"] == "local"


def test_oversized_prompt_is_not_sent_in_truncated_form(home, monkeypatch):
    monkeypatch.setattr(jev, "call_api", lambda *a: pytest.fail("oversized prompt reached API"))
    assert evaluate(home, prompt="x" * 10_001)["reason"] == "prompt_too_large"


def test_missing_key_is_observable_without_losing_routing(home, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert evaluate(home)["reason"] == "missing_api_key"
    assert logs(home)[0]["decision"]["tier"] == "standard"


def test_credentials_environment_and_private_file(home, monkeypatch):
    assert jev.api_key(home) == "test-key-never-log-this"
    monkeypatch.delenv("TYPESAFE_API_KEY")
    path = home / "jev-api-key"
    path.write_text("local-key")
    path.chmod(0o600)
    assert jev.api_key(home) == "local-key"
    path.chmod(0o644)
    assert jev.api_key(home) == ""
    path.chmod(0o600)
    for bad in (b"x" * 1025, b"key\ninjection", b"\xff"):
        path.write_bytes(bad)
        assert jev.api_key(home) == ""
    path.unlink()
    path.symlink_to(home / "config.json")
    assert jev.api_key(home) == ""


@pytest.mark.parametrize("bad", [None, [], {}, {"answers": []}, {"answers": {"routing_tier": []}}])
def test_bad_response_envelope(home, monkeypatch, bad):
    monkeypatch.setattr(jev, "call_api", lambda *a: {"response": bad})
    assert evaluate(home)["reason"] == "invalid_response"


@pytest.mark.parametrize("change", [
    {"type": "score"}, {"choice": "unknown"}, {"choice": []}, {"confidence": True},
    {"confidence": float("nan")}, {"confidence": 2}, {"probabilities": []},
    {"probabilities": {"simple": 1}},
    {"probabilities": {"simple": 0.5, "standard": 0.5, "complex": 0.5}},
    {"probabilities": {"simple": True, "standard": 0, "complex": 0}},
    {"probabilities": {"simple": 0.9, "standard": 0.05, "complex": 0.05}},
])
def test_invalid_choice_cannot_route(change):
    data = response()
    data["answers"]["routing_tier"].update(change)
    assert jev.parse_answer(data, CANDIDATES) is None


@pytest.mark.parametrize("error", ["timeout", "http_error", "invalid_json", "transport_error"])
def test_service_failures_fall_back_and_are_logged(home, monkeypatch, error):
    monkeypatch.setattr(jev, "call_api", lambda *a: {"error": error, "http_status": 429})
    assert evaluate(home)["reason"] == error
    assert logs(home)[-1]["http_status"] == 429


def test_metadata_default_and_credential_redaction(home, monkeypatch):
    evaluate(home, {**CONFIG, "jev": {"enabled": True, "scope": "all"}})
    request, reply = logs(home)
    assert "request" not in request and "response" not in reply
    key = os.environ["TYPESAFE_API_KEY"]
    body = response()
    body["echo"] = key + " Bearer another-secret sk-abcdefghijklmnop"
    monkeypatch.setattr(jev, "call_api", lambda *a: {"response": body})
    prompt = "synthetic task $(touch /tmp/never)\nsecond line"
    evaluate(home, prompt=prompt)
    text = (home / "logs" / "jev.jsonl").read_text()
    assert key not in text and "another-secret" not in text and "sk-abcdefghijklmnop" not in text
    assert len(logs(home)) == 4
    assert "$(touch /tmp/never)" in text


def test_rotation_and_logging_failures_do_not_change_decisions(home, monkeypatch):
    evaluate(home)
    monkeypatch.setattr(jev, "LOG_MAX_BYTES", 1)
    evaluate(home)
    assert (home / "logs" / "jev.jsonl.1").exists()
    (home / "logs" / "jev.jsonl").unlink()
    outside = home / "untouched"
    outside.write_text("untouched")
    (home / "logs" / "jev.jsonl").symlink_to(outside)
    assert evaluate(home)["source"] == "jev"
    assert outside.read_text() == "untouched"


def test_log_directory_symlink_and_nonregular_file_are_refused(home):
    outside = home / "outside"
    outside.mkdir()
    (home / "logs").symlink_to(outside, target_is_directory=True)
    jev.write_log(home, {"event": "test"})
    assert not list(outside.iterdir())
    (home / "logs").unlink()
    (home / "logs").mkdir()
    os.mkfifo(home / "logs" / "jev.jsonl")
    jev.write_log(home, {"event": "test"})  # O_NONBLOCK: never hangs on a FIFO


def test_two_tier_candidates_do_not_offer_unavailable_opus():
    body = jev.build_request("hello", {"simple": "sonnet", "complex": "fable"})
    assert set(body["questions"]["routing_tier"]["criteria"]) == {"simple", "complex"}


def test_http_contract(monkeypatch):
    seen = {}
    class Reply(io.BytesIO):
        status = 200
    class Opener:
        def open(self, req, timeout):
            seen.update(url=req.full_url, headers=req.headers, body=json.loads(req.data), timeout=timeout)
            return Reply(json.dumps(response()).encode())
    monkeypatch.setattr(jev, "build_opener", lambda *a: Opener())
    body = jev.build_request("a prompt", CANDIDATES)
    assert jev._http(body, "test-secret", 1)["response"] == response()
    assert seen == {"url": jev.ENDPOINT, "headers": {
        "Authorization": "Bearer test-secret", "Content-type": "application/json", "Accept": "application/json",
    }, "body": body, "timeout": 1}
    assert jev._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test") is None


@pytest.mark.parametrize("status,raw,error", [
    (401, b'{"message":"bad key"}', "http_error"), (429, b'{}', "http_error"),
    (200, b'not JSON', "invalid_json"), (200, b'{', "invalid_json"),
    (200, b'x' * (jev.RESPONSE_MAX_BYTES + 1), "response_too_large"),
])
def test_http_errors_are_bounded(monkeypatch, status, raw, error):
    class Reply(io.BytesIO):
        pass
    class Opener:
        def open(self, req, timeout):
            if status != 200:
                raise HTTPError(req.full_url, status, "error", {}, io.BytesIO(raw))
            reply = Reply(raw)
            reply.status = status
            return reply
    monkeypatch.setattr(jev, "build_opener", lambda *a: Opener())
    assert jev._http({}, "key", 1)["error"] == error


def test_worker_sanitizes_transport_errors_and_returns_json(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"request": {}, "timeout": 1})))
    monkeypatch.setenv("TYPESAFE_API_KEY", "key")
    monkeypatch.setattr(jev, "_http", lambda *a: {"response": response()})
    assert jev.worker() == 0
    assert json.loads(capsys.readouterr().out)["response"] == response()
    monkeypatch.setattr("sys.stdin", io.StringIO("broken"))
    assert jev.worker() == 0
    assert json.loads(capsys.readouterr().out) == {"error": "transport_error"}


@pytest.mark.parametrize("reply,expected", [
    (SimpleNamespace(returncode=0, stdout='{"http_status": 200}'), {"http_status": 200}),
    (SimpleNamespace(returncode=1, stdout='secret'), {"error": "worker_error"}),
    (SimpleNamespace(returncode=0, stdout='[]'), {"error": "worker_error"}),
    (SimpleNamespace(returncode=0, stdout='{'), {"error": "worker_error"}),
    (subprocess.TimeoutExpired("worker", 0.1), {"error": "timeout"}),
    (OSError("secret"), {"error": "worker_error"}),
])
def test_subprocess_deadline_and_credentials(monkeypatch, reply, expected):
    def run(args, **kwargs):
        assert kwargs["timeout"] == 0.1
        assert "key-secret" not in " ".join(args) + kwargs["input"]
        assert kwargs["env"]["TYPESAFE_API_KEY"] == "key-secret"
        assert "shell" not in kwargs
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(jev.subprocess, "run", run)
    assert jev.call_api({}, "key-secret", 0.1) == expected


def test_hook_and_agent_share_jev_decision(home, monkeypatch):
    (home / "config.json").write_text(json.dumps(CONFIG))
    agents = home.parent / "agents"
    agents.mkdir()
    (agents / "mid-task-opus.md").write_text("agent")
    monkeypatch.setattr(jev, "call_api", lambda *a: {"response": response("standard")})
    out = router.run(json.dumps({"prompt": "a task", "session_id": "../../evil"}))
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "mid-task-opus" in context and "Jev tier evaluation" in context
    tool = {"subagent_type": "general-purpose", "prompt": "a task"}
    target, reason = agent_router.decide(tool, CONFIG, agents)
    assert target == "mid-task-opus" and "evaluated by Jev" in reason
    assert logs(home)[-1]["origin"] == "agent"
    assert not (home / "state").exists()


def test_no_jev_calls_when_routing_is_skipped(home, monkeypatch):
    monkeypatch.setattr(jev, "call_api", lambda *a: pytest.fail("skipped routing called Jev"))
    (home / "config.json").write_text(json.dumps(CONFIG))
    for payload in ({"prompt": "/help"}, {"prompt": "hello", "agent_id": "child"}, {"prompt": 1}):
        assert router.run(json.dumps(payload)) == ""
    assert agent_router.decide({"subagent_type": "Explore", "prompt": "task"}, CONFIG, home) is None
    off = {**CONFIG, "routing": {"enabled": False}}
    (home / "config.json").write_text(json.dumps(off))
    assert router.run('{"prompt":"a task"}') == ""
    assert router.evaluate_route("a task", off)["source"] == "local"
    assert router.evaluate_route("a task", {**CONFIG, "models": {}})["source"] == "local"


def test_project_cannot_enable_jev_but_can_disable_it(home):
    assert router.merge_project_config({}, {"jev": CONFIG["jev"]}).get("jev") is None
    config = router.merge_project_config(CONFIG, {"jev": {"enabled": False}})
    assert router.evaluate_route("task", config)["source"] == "local"
    assert logs(home)[0]["jev"]["status"] == "disabled"
    assert "prompt" not in logs(home)[0]


def test_optional_evaluator_bug_does_not_suppress_local_routing(home, monkeypatch, caplog):
    def broken(*a, **kwargs):
        raise RuntimeError("private prompt text")
    monkeypatch.setattr(jev, "evaluate", broken)
    assert router.evaluate_route("refactor everything", CONFIG)["reason"] == "evaluation_error"
    assert "private prompt text" not in caplog.text


def test_cli_preview_live_and_offline_explain(home, monkeypatch, capsys):
    (home / "config.json").write_text(json.dumps(CONFIG))
    monkeypatch.setattr("sys.stdin", io.StringIO("explain this"))
    assert cli.main(["jev", "--offline"]) == 0
    assert json.loads(capsys.readouterr().out)["state"]["available_models"] == CANDIDATES
    assert not (home / "logs").exists()
    monkeypatch.setattr("sys.stdin", io.StringIO("explain this"))
    assert cli.main(["jev"]) == 0
    assert '"source": "jev"' in capsys.readouterr().out
    monkeypatch.setattr(jev, "call_api", lambda *a: pytest.fail("explain reached API"))
    assert cli.main(["explain", "what is this?"]) == 0
    assert "Local baseline only" in capsys.readouterr().out


def test_cli_reports_invalid_inputs_and_missing_key(home, monkeypatch, capsys):
    assert cli.main(["jev"]) == 2
    (home / "config.json").write_text(json.dumps(CONFIG))
    for prompt in ("", "x" * 10_001):
        monkeypatch.setattr("sys.stdin", io.StringIO(prompt))
        assert cli.main(["jev"]) == 2
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.setattr("sys.stdin", io.StringIO("hello"))
    assert cli.main(["jev"]) == 1
    assert "missing_api_key" in capsys.readouterr().out
    (home / "config.json").write_text('{}')
    monkeypatch.setattr("sys.stdin", io.StringIO("hello"))
    assert cli.main(["jev"]) == 2
    (home / "config.json").write_text(json.dumps({**CONFIG, "jev": None}))
    monkeypatch.setattr("sys.stdin", io.StringIO("hello"))
    assert cli.main(["jev", "--offline"]) == 0


def test_status_shows_jev_state_without_key(home, capsys):
    import status_report
    status_report.render_summary(CONFIG, home, "sonnet")
    text = capsys.readouterr().out
    assert "route; API key available; logs content" in text
    assert os.environ["TYPESAFE_API_KEY"] not in text


def test_real_worker_is_killed_at_deadline(tmp_path, monkeypatch):
    # Exercise subprocess.run's actual kill/reap path, not a mocked TimeoutExpired.
    import time
    script = tmp_path / "slow_worker.py"
    script.write_text('import time\ntime.sleep(20)\n')
    monkeypatch.setattr(jev, "__file__", str(script))
    started = time.monotonic()
    assert jev.call_api({}, "key", 0.1) == {"error": "timeout"}
    assert time.monotonic() - started < 3


def test_new_config_reaches_all_three_local_tiers():
    from pathlib import Path
    config = json.loads((Path(__file__).parents[1] / 'config/config.example.json').read_text())
    assert config['models'] == CANDIDATES
    assert [router.select_tier(score, config) for score in (2, 3, 6, 7)] == [None, 'standard', 'standard', 'complex']


@pytest.mark.parametrize('mode,confidence,reason,source', [
    ('route', 0.99, 'accepted', 'jev'),
    ('shadow', 0.99, 'shadow', 'local'),
    ('route', 0.2, 'low_confidence', 'local'),
])
def test_logs_keep_independent_verdicts_and_scoring_breakdown(home, monkeypatch, mode, confidence, reason, source):
    monkeypatch.setattr(router, 'load_classifier', lambda: {'terms': {'validation': 1.5}})
    monkeypatch.setattr(jev, 'call_api', lambda *a: {'response': response('simple', confidence), 'http_status': 200})
    config = {**CONFIG, 'jev': {**CONFIG['jev'], 'mode': mode}}
    prompt = 'Add validation to this API endpoint and tests'
    result = router.evaluate_route(prompt, config)
    entry = logs(home)[-1]
    detail = router.analyse_prompt(prompt, router.load_classifier())
    assert entry['schema_version'] == 2
    assert entry['offline']['score'] == detail['score']
    assert entry['offline']['base_score'] == detail['base']
    assert entry['offline']['learned_adjustment'] == 1.5
    assert entry['offline']['matched_terms'] == {'validation': 1.5}
    assert entry['offline']['classifier_loaded'] is True
    assert entry['offline']['signals']
    assert entry['offline']['thresholds'] == {'standard': 3, 'complex': 7}
    assert entry['offline']['tier'] == 'standard'
    assert entry['offline']['model'] == 'opus'
    assert entry['jev']['tier'] == 'simple' and entry['jev']['model'] == 'sonnet'
    assert entry['jev']['status'] == reason and entry['jev']['confidence'] == confidence
    assert entry['agreement'] is False
    assert entry['decision']['source'] == result['source'] == source
    assert entry['final_model'] == ('sonnet' if source == 'jev' else 'opus')
    assert entry['prompt'] == prompt


def test_comparison_when_evaluators_agree_and_local_is_simple(home, monkeypatch):
    monkeypatch.setattr(jev, 'call_api', lambda *a: {'response': response('simple')})
    router.evaluate_route('What is Python?', CONFIG)
    entry = logs(home)[-1]
    assert entry['agreement'] is True and entry['offline']['tier'] == 'simple'
    assert entry['offline']['caps'] == ['short question', 'definitional question']


def test_failure_logs_offline_explanation_without_a_fabricated_jev_choice(home, monkeypatch):
    def broken(*a):
        raise ValueError('private input must not leak')
    monkeypatch.setattr(jev, 'call_api', broken)
    result = router.evaluate_route('refactor the service', CONFIG)
    entry = logs(home)[-1]
    assert entry['agreement'] is None
    assert 'tier' not in entry['jev']
    assert entry['jev']['status'] == 'evaluation_error'
    assert entry['offline']['score'] == result['score']
    assert 'private input must not leak' not in (home/'logs/jev.jsonl').read_text()


def test_metadata_hides_matched_terms_and_prompt_but_preserves_both_results(home, monkeypatch):
    monkeypatch.setattr(router, 'load_classifier', lambda: {'terms': {'confidential': 1.0}})
    config = {**CONFIG, 'jev': {**CONFIG['jev'], 'log_content': False}}
    router.evaluate_route('refactor confidential module', config)
    entry = logs(home)[-1]
    assert 'matched_terms' not in entry['offline'] and 'signals' not in entry['offline']
    assert 'prompt' not in entry and 'response' not in entry
    assert 'confidential' not in (home/'logs/jev.jsonl').read_text()
    assert entry['offline']['learned_adjustment'] == 1.0
    assert entry['jev']['tier'] == 'complex'


@pytest.mark.parametrize('content', [False, True])
def test_offline_result_is_logged_before_jev_call_starts(home, monkeypatch, content):
    prompt = 'Add validation to this API endpoint and tests'
    config = {**CONFIG, 'jev': {**CONFIG['jev'], 'log_content': content}}

    def inspect_in_flight(body, key, timeout):
        entries = logs(home)
        assert len(entries) == 1
        entry = entries[0]
        assert entry['schema_version'] == 2 and entry['event'] == 'jev.request'
        assert entry['offline']['model'] == 'opus'
        assert entry['offline']['tier'] == 'standard'
        assert entry['offline']['score'] >= 3
        assert entry['jev']['status'] == 'evaluating'
        assert 'decision' not in entry and 'response' not in entry
        assert ('prompt' in entry) is content
        assert ('signals' in entry['offline']) is content
        assert ('request' in entry) is content
        assert 'offline' not in body['state']  # Jev still evaluates independently
        return {'response': response('simple'), 'http_status': 200}

    monkeypatch.setattr(jev, 'call_api', inspect_in_flight)
    router.evaluate_route(prompt, config)
    start, end = logs(home)
    assert end['jev']['status'] == 'accepted'
    assert start['request_id'] == end['request_id']
    assert start['offline'] == end['offline']


def test_disabled_routing_has_no_evaluation_or_decision_log(home, monkeypatch):
    monkeypatch.setattr(jev, 'evaluate', lambda *a, **kw: pytest.fail('routing is off'))
    config = {**CONFIG, 'routing': {'enabled': False}}
    (home/'config.json').write_text(json.dumps(config))
    assert router.run(json.dumps({'prompt': 'refactor the service'})) == ''
    assert not (home/'logs').exists()


@pytest.mark.parametrize('mode,confidence,source', [('route', .9, 'Jev'), ('shadow', .9, 'offline'),
                                                  ('route', .2, 'offline')])
def test_session_notice_shows_both_evaluations_separate_from_context(home, monkeypatch, mode, confidence, source):
    config = {**CONFIG, 'routing': {'show_decisions': True}, 'jev': {**CONFIG['jev'], 'mode': mode}}
    (home/'config.json').write_text(json.dumps(config))
    monkeypatch.setattr(jev, 'call_api', lambda *a: {'response': response('simple', confidence)})
    prompt = 'Add validation to this API endpoint and tests'
    output = json.loads(router.run(json.dumps({'prompt': prompt})))
    message = output['systemMessage']
    assert 'Offline: opus' in message and 'Jev: sonnet' in message and 'DISAGREE' in message
    assert f'via {source}' in message
    assert f'{confidence:.0%}' in message
    assert prompt not in message and 'test-key-never-log-this' not in message
    if source == 'Jev':
        assert 'hookSpecificOutput' not in output  # simple route still produces a visible notice
    else:
        assert 'mid-task-opus' in output['hookSpecificOutput']['additionalContext']
        assert message not in output['hookSpecificOutput']['additionalContext']


@pytest.mark.parametrize('error', ['timeout', 'http_error', 'missing_api_key', 'disabled', 'evaluation_error'])
def test_session_notice_explains_offline_fallback(home, monkeypatch, error):
    config = {**CONFIG, 'routing': {'show_decisions': True}}
    if error == 'disabled':
        config['jev'] = {'enabled': False}
    elif error == 'missing_api_key':
        monkeypatch.setattr(jev, 'api_key', lambda *a: '')
    elif error == 'evaluation_error':
        def broken(*a, **kw):
            raise ValueError('secret should not be displayed')
        monkeypatch.setattr(jev, 'evaluate', broken)
    else:
        monkeypatch.setattr(jev, 'call_api', lambda *a: {'error': error})
    (home/'config.json').write_text(json.dumps(config))
    output = json.loads(router.run(json.dumps({'prompt': 'What is a tuple?'})))
    assert f'Jev: {error}' in output['systemMessage']
    assert 'Selected: sonnet via offline' in output['systemMessage']
    assert 'secret' not in output['systemMessage']


@pytest.mark.parametrize('value', [False, 'true', 1, None])
def test_session_notice_can_be_disabled_without_disabling_logs(home, monkeypatch, value):
    config = {**CONFIG, 'routing': {'show_decisions': value}}
    (home/'config.json').write_text(json.dumps(config))
    monkeypatch.setattr(jev, 'call_api', lambda *a: {'response': response('simple')})
    assert router.run(json.dumps({'prompt': 'What is a tuple?'})) == ''
    assert logs(home)[-1]['final_model'] == 'sonnet'


def test_visible_notices_respect_routing_exclusions(home, monkeypatch):
    config = {**CONFIG, 'routing': {'show_decisions': True}}
    (home/'config.json').write_text(json.dumps(config))
    monkeypatch.setattr(jev, 'call_api', lambda *a: pytest.fail('excluded request called Jev'))
    for payload in [{'prompt': '/help'}, {'prompt': 'hello', 'agent_id': 'child'}]:
        assert router.run(json.dumps(payload)) == ''
    config['routing']['enabled'] = False
    (home/'config.json').write_text(json.dumps(config))
    assert router.run(json.dumps({'prompt': 'hello'})) == ''


@pytest.mark.parametrize('choice,agent_present', [('standard', True), ('standard', False), ('simple', False)])
def test_agent_notice_appears_even_when_no_rewrite_is_needed(home, monkeypatch, choice, agent_present):
    config = {**CONFIG, 'routing': {'show_decisions': True}}
    (home/'config.json').write_text(json.dumps(config))
    agents = home.parent/'agents'
    agents.mkdir()
    if agent_present:
        (agents/'mid-task-opus.md').write_text('agent')
    monkeypatch.setattr(jev, 'call_api', lambda *a: {'response': response(choice)})
    payload = {'tool_name': 'Task', 'tool_input': {'subagent_type': 'general-purpose', 'prompt': 'What is a tuple?'}}
    output = json.loads(agent_router.run(json.dumps(payload)))
    assert 'Offline: sonnet' in output['systemMessage']
    if agent_present:
        assert output['hookSpecificOutput']['updatedInput']['subagent_type'] == 'mid-task-opus'
    else:
        assert output['systemMessage'].endswith('Agent call unchanged.')
        assert 'hookSpecificOutput' not in output
    payload['tool_input']['subagent_type'] = 'Explore'
    assert agent_router.run(json.dumps(payload)) == ''


def test_display_failures_do_not_change_the_route(home):
    def broken(event):
        raise ValueError('private error')
    result = jev.evaluate('hello', CONFIG, home, CANDIDATES, LOCAL, 'prompt', on_result=broken)
    assert result['tier'] == 'complex' and result['source'] == 'jev'


def test_session_message_escapes_terminal_control_characters(home):
    evaluate(home)
    event = logs(home)[-1]
    event['offline']['model'] = 'x\x1b]52;c;evil\x07\n'
    event['jev']['model'] = 'y\x1b[2J'
    message = jev.session_message(event)
    assert '\x1b' not in message and '\x07' not in message and '\n' not in message
    assert '\\u001b' in message


def exchange_config(content=True, show=True):
    return {**CONFIG, 'routing': {'show_decisions': True, 'show_exchange': show},
            'jev': {**CONFIG['jev'], 'log_content': content}}


def test_in_session_exchange_contains_exact_sent_request_and_received_response(home, monkeypatch):
    config = exchange_config()
    (home/'config.json').write_text(json.dumps(config))
    sent = []
    reply = response('simple')

    def call(body, key, timeout):
        sent.append(body)
        return {'response': reply, 'http_status': 200}

    monkeypatch.setattr(jev, 'call_api', call)
    prompt = 'Does this utility support OpenAI?'
    output = json.loads(router.run(json.dumps({'prompt': prompt})))
    notice = output['systemMessage']
    assert len(sent) == 1
    assert json.dumps(sent[0], indent=2) in notice
    assert json.dumps(reply, indent=2) in notice
    assert f'Jev request (POST {jev.ENDPOINT})' in notice
    assert 'Jev response (HTTP 200)' in notice
    assert logs(home)[-1]['request_id'] in notice
    assert 'hookSpecificOutput' not in output  # body is for the user, not a model instruction
    assert 'request' not in logs(home)[-1]  # result log does not duplicate the request body


@pytest.mark.parametrize('content,show', [(False, True), (True, False), (True, 'true'), (True, 1)])
def test_session_exchange_requires_both_content_and_display_opt_ins(home, monkeypatch, content, show):
    (home/'config.json').write_text(json.dumps(exchange_config(content, show)))
    monkeypatch.setattr(jev, 'call_api', lambda *a: {'response': response('simple')})
    notice = json.loads(router.run(json.dumps({'prompt': 'private prompt'})))['systemMessage']
    assert 'private prompt' not in notice and '"answers"' not in notice
    if content is False:
        assert 'content unavailable' in notice


def test_session_exchange_redacts_credentials_and_escapes_control_characters(home, monkeypatch):
    (home/'config.json').write_text(json.dumps(exchange_config()))
    key = 'test-key-never-log-this'
    reply = response('simple')
    reply['untrusted'] = key + ' Bearer abcdef123456 \x1b]52;c;payload\x07\n'
    monkeypatch.setattr(jev, 'call_api', lambda *a: {'response': reply})
    notice = json.loads(router.run(json.dumps({'prompt': 'hello'})))['systemMessage']
    assert key not in notice and 'abcdef123456' not in notice and '[REDACTED]' in notice
    assert '\x1b' not in notice and '\x07' not in notice and '\\u001b' in notice


def test_large_exchange_keeps_both_bodies_and_trace_within_claude_notice_limit(home, monkeypatch):
    (home/'config.json').write_text(json.dumps(exchange_config()))
    reply = response('simple')
    reply['large_field'] = 'z'*50_000
    monkeypatch.setattr(jev, 'call_api', lambda *a: {'response': reply, 'http_status': 200})
    notice = json.loads(router.run(json.dumps({'prompt': 'x'*9000})))['systemMessage']
    assert len(notice) < 10_000
    assert notice.count('[truncated; full content is in the local log]') == 2
    assert 'Jev request' in notice and 'Jev response' in notice and 'request_id:' in notice
    start, end = logs(home)
    assert len(start['request']['state']['prompt']) == 9000
    assert len(end['response']['large_field']) == 50_000


@pytest.mark.parametrize('error', ['missing_api_key', 'timeout'])
def test_exchange_distinguishes_unsent_request_from_missing_response(home, monkeypatch, error):
    (home/'config.json').write_text(json.dumps(exchange_config()))
    if error == 'missing_api_key':
        monkeypatch.setattr(jev, 'api_key', lambda *a: '')
    else:
        monkeypatch.setattr(jev, 'call_api', lambda *a: {'error': error})
    notice = json.loads(router.run(json.dumps({'prompt': 'What is a tuple?'})))['systemMessage']
    if error == 'missing_api_key':
        assert 'content unavailable' in notice
        assert 'What is a tuple?' not in notice
    else:
        assert 'No response body recorded' in notice
        assert 'What is a tuple?' in notice


def test_agent_exchange_is_also_visible_without_injecting_it_into_context(home, monkeypatch):
    (home/'config.json').write_text(json.dumps(exchange_config()))
    monkeypatch.setattr(jev, 'call_api', lambda *a: {'response': response('simple')})
    output = json.loads(agent_router.run(json.dumps({'tool_name': 'Task', 'tool_input': {
        'subagent_type': 'general-purpose', 'prompt': 'Explain Python tuples'}})))
    assert '"prompt": "Explain Python tuples"' in output['systemMessage']
    assert 'Jev response' in output['systemMessage']
    assert 'hookSpecificOutput' not in output


def test_project_can_hide_exchange_but_cannot_enable_it():
    enabled = exchange_config()
    hidden = router.merge_project_config(enabled, {'routing': {'show_exchange': False}})
    assert hidden['routing']['show_exchange'] is False
    disabled = exchange_config(show=False)
    unchanged = router.merge_project_config(disabled, {'routing': {'show_exchange': True}})
    assert unchanged['routing']['show_exchange'] is False


def test_oversized_prompt_records_only_metadata_and_skipped_reason(home):
    router.evaluate_route('x' * 20_000, CONFIG)
    entry = logs(home)[-1]
    assert 'prompt' not in entry and 'request' not in entry
    assert entry['jev']['status'] == 'prompt_too_large'
    assert entry['agreement'] is None
