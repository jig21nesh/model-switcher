import json
import os
import select
import signal
import subprocess
import time
from pathlib import Path

import pytest

import cli
import routing_report as report

ID = 'a' * 32
OTHER = 'b' * 32


def result(request_id=ID):
    return {
        'request_id': request_id, 'event': 'jev.result', 'timestamp': 1790634839.9079301,
        'origin': 'prompt', 'prompt': 'Add input validation',
        'offline': {'model': 'opus', 'tier': 'standard', 'score': 4, 'base_score': 3,
                    'learned_adjustment': 1, 'classifier_loaded': True,
                    'signals': [['domain terms (api)', 1]], 'matched_terms': {'validation': 1},
                    'caps': ['test cap'], 'thresholds': {'standard': 3, 'complex': 7}},
        'jev': {'tier': 'simple', 'model': 'sonnet', 'confidence': .99, 'status': 'accepted'},
        'agreement': False, 'decision': {'tier': None, 'source': 'jev', 'reason': 'accepted'},
        'final_model': 'sonnet', 'latency_ms': 450,
    }


def test_reader_compares_results_and_prints_full_exchange(tmp_path):
    path = tmp_path/'jev.jsonl'
    request = {'request_id': ID, 'event': 'jev.request', 'request': {'state': 'prompt'}}
    path.write_text('\n'.join(json.dumps(r) for r in [request, result()]) + '\n')
    output = []
    assert report.render(path, echo=output.append) == 0
    text = '\n'.join(output)
    for part in ('Offline: opus (standard)', 'Jev: sonnet (simple)', 'Final: sonnet via jev',
                 'DISAGREE', 'base 3, learned 1', 'Signals:', 'Learned terms:', 'Caps:', ID):
        assert part in text
    output = []
    assert report.render(path, as_json=True, request_id=ID, echo=output.append) == 0
    assert [json.loads(line) for line in output] == [request, result()]


def test_reader_includes_backup_and_limits_by_decisions(tmp_path):
    path = tmp_path/'jev.jsonl'
    path.with_name(path.name+'.1').write_text(json.dumps(result())+'\n')
    path.write_text(json.dumps(result(OTHER))+'\n')
    out = []
    report.render(path, limit=1, as_json=True, echo=out.append)
    assert [json.loads(r)['request_id'] for r in out] == [OTHER]
    out = []
    report.render(path, request_id=ID, as_json=True, echo=out.append)
    assert [json.loads(r)['request_id'] for r in out] == [ID]


def test_reader_ignores_malformed_lines_and_bounds_reads(tmp_path, monkeypatch):
    path = tmp_path/'jev.jsonl'
    valid = json.dumps(result())+'\n'
    path.write_bytes(b'not json\n[]\n{"request_id":4}\n\xff\n' + valid.encode() + b'{')
    assert report._read(path) == [result()]
    path.write_text('x'*5000+'\n'+valid)
    monkeypatch.setattr(report, 'READ_MAX_BYTES', len(valid.encode())+10)
    assert report._read(path) == [result()]


def test_reader_skips_missing_files_symlinks_and_fifo(tmp_path):
    assert report._read(tmp_path/'missing') == []
    actual = tmp_path/'actual'
    actual.write_text(json.dumps(result()))
    path = tmp_path/'jev.jsonl'
    path.symlink_to(actual)
    assert report._read(path) == []
    path.unlink()
    os.mkfifo(path)
    assert report._read(path) == []
    out = []
    assert report.render(tmp_path/'missing', echo=out.append) == 0
    assert 'No matching routing decisions' in out[0]


@pytest.mark.parametrize('limit,request_id', [(0, None), (101, None), (10, '../secret'), (10, 'x')])
def test_invalid_filters_do_not_read_files(tmp_path, monkeypatch, limit, request_id):
    monkeypatch.setattr(report, '_read', lambda *a: pytest.fail('invalid filter read a file'))
    assert report.render(tmp_path/'jev.jsonl', limit, request_id=request_id, echo=lambda s: None) == 2


def test_legacy_records_do_not_fabricate_the_offline_tier():
    record = {'request_id': ID, 'event': 'jev.result', 'decision': {
        'score': 3, 'tier': 'complex', 'source': 'jev', 'reason': 'accepted',
        'jev': {'choice': 'complex', 'confidence': .9}}, 'timestamp': None}
    out = []
    report.describe(record, out.append)
    text = '\n'.join(out)
    assert 'tier unavailable in this older log record' in text
    assert 'unknown time' in text and 'not comparable' in text and 'confidence 0.9' in text


def test_terminal_escapes_are_not_executed():
    record = result()
    record.update(prompt='\x1b]52;c;payload\x07'+'x'*300, timestamp=float('nan'), agreement=True)
    record['offline'] = None
    record['jev'] = {'status': 'timeout'}
    out = []
    report.describe(record, out.append)
    text = '\n'.join(out)
    assert '\x1b' not in text and '\x07' not in text
    assert '\\u001b' in text and '...' in text
    assert 'Jev: timeout' in text and 'evaluators agree' in text


def test_cli_logs_is_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('MODEL_SWITCHER_HOME', str(tmp_path))
    monkeypatch.setattr('jev_router.call_api', lambda *a: pytest.fail('logs invoked the API'))
    (tmp_path/'logs').mkdir()
    (tmp_path/'logs/jev.jsonl').write_text(json.dumps(result())+'\n')
    assert cli.main(['logs', '--limit', '1']) == 0
    assert 'Offline: opus' in capsys.readouterr().out
    assert cli.main(['logs', '--json', '--request-id', ID]) == 0
    assert json.loads(capsys.readouterr().out) == result()


def request(request_id=ID):
    record = result(request_id)
    return {k: v for k, v in dict(record, event='jev.request',
                                 jev={'status': 'evaluating', 'evaluation_model': 'jev-1.13.0',
                                      'mode': 'route'}).items()
            if k not in ('decision', 'final_model', 'agreement', 'latency_ms')}


def append(path, record):
    with path.open('a') as stream:
        stream.write(json.dumps(record) + '\n')


def ticks(monkeypatch, *actions):
    actions = iter(actions)

    def sleep(_):
        action = next(actions, None)
        if action is None:
            raise KeyboardInterrupt
        action()

    monkeypatch.setattr(report.time, 'sleep', sleep)


def test_follow_prints_pending_before_response_and_does_not_repeat_idle_records(tmp_path, monkeypatch):
    path = tmp_path/'jev.jsonl'
    append(path, request())
    out = []

    def respond():
        text = '\n'.join(out)
        assert 'Offline: opus (standard)' in text
        assert 'Jev: evaluating... (jev-1.13.0; route mode)' in text
        assert 'Final: pending' in text and 'Final: sonnet' not in text
        append(path, result())

    ticks(monkeypatch, lambda: None, respond, lambda: None)
    assert report.render(path, follow=True, echo=out.append) == 0
    text = '\n'.join(out)
    assert text.count('Final: pending') == text.count('Final: sonnet') == 1
    assert text.count('DISAGREE') == 1


def test_follow_waits_for_file_and_complete_lines_then_survives_rotation(tmp_path, monkeypatch):
    path = tmp_path/'jev.jsonl'
    out = []

    def incomplete():
        path.write_text(json.dumps(request()))  # complete JSON still needs its newline

    def finish():
        assert not out
        with path.open('a') as stream:
            stream.write('\ninvalid JSON\n')

    def rotate():
        assert [json.loads(s)['event'] for s in out] == ['jev.request']
        path.rename(path.with_name('jev.jsonl.1'))
        append(path, result())

    def truncate():
        path.write_text('')
        append(path, result(OTHER))

    ticks(monkeypatch, incomplete, finish, rotate, truncate)
    assert report.render(path, as_json=True, follow=True, echo=out.append) == 0
    assert [(r['request_id'], r['event']) for r in map(json.loads, out)] == [
        (ID, 'jev.request'), (ID, 'jev.result'), (OTHER, 'jev.result')]


def test_follow_filters_live_events_and_ignores_unknown_events(tmp_path, monkeypatch):
    path = tmp_path/'jev.jsonl'
    append(path, request(OTHER))
    append(path, {'request_id': ID, 'event': ['hostile', 'shape']})
    out = []

    def update():
        append(path, result(OTHER))
        append(path, request())
        append(path, {'request_id': ID, 'event': 'unknown'})
        append(path, result())

    ticks(monkeypatch, update)
    report.render(path, request_id=ID, follow=True, as_json=True, echo=out.append)
    assert list(map(json.loads, out)) == [request(), result()]


@pytest.mark.parametrize('as_json', [False, True])
def test_follow_limits_completed_history_but_includes_pending_request(tmp_path, monkeypatch, as_json):
    path = tmp_path/'jev.jsonl'
    pending_id = 'c'*32
    for record in [request(), result(), request(OTHER), result(OTHER), request(pending_id)]:
        append(path, record)
    ticks(monkeypatch)
    out = []
    report.render(path, limit=1, follow=True, as_json=as_json, echo=out.append)
    text = '\n'.join(out)
    assert ID not in text and OTHER in text and pending_id in text
    if as_json:
        assert list(map(json.loads, out)) == [request(OTHER), result(OTHER), request(pending_id)]
    else:
        assert text.count('Final: pending') == text.count('Final: sonnet') == 1


def test_follow_idle_does_not_reread_files(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(report, '_read', lambda p: calls.append(p) or [])
    ticks(monkeypatch, lambda: None, lambda: None)
    report.render(tmp_path/'missing', follow=True, as_json=True)
    assert len(calls) == 4  # initial history + first follow snapshot, none during idle polls


def test_follow_cli_flushes_request_before_result_is_written(tmp_path):
    path = tmp_path/'logs/jev.jsonl'
    path.parent.mkdir()
    append(path, request())
    command = Path(__file__).parents[1]/'bin/model-switcher'
    process = subprocess.Popen([str(command), 'logs', '-f', '--json'], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env={**os.environ, 'MODEL_SWITCHER_HOME': str(tmp_path)})

    def next_line():
        data = b''
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], 0.1)[0]:
                chunk = os.read(process.stdout.fileno(), 65536)
                assert chunk, 'viewer exited before printing a record'
                data += chunk
                if data.endswith(b'\n'):
                    return json.loads(data)
        pytest.fail('viewer did not flush output while still running')

    try:
        assert next_line() == request()
        assert process.poll() is None
        append(path, result())
        assert next_line() == result()
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=5)
        assert process.returncode == 0 and stderr == b''
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
