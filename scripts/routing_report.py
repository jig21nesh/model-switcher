"""Read local routing traces without issuing a model request or printing terminal control codes."""

import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

READ_MAX_BYTES = 3 * 1024 * 1024
RESULT_EVENTS = ("jev.result", "routing.decision")


def _read(path: Path) -> list[dict]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return []
            offset = max(0, info.st_size - READ_MAX_BYTES)
            stream.seek(offset)
            raw = stream.read(READ_MAX_BYTES)
    except OSError:
        return []
    # A writer may still be appending a record. Wait for its newline before displaying it.
    lines = raw.split(b"\n")[:-1]
    if offset:
        lines = lines[1:]  # the tail may begin partway through a record
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict) and isinstance(record.get("request_id"), str):
            records.append(record)
    return records


def _display(value: object, limit: int = 180) -> str:
    # JSON escaping keeps prompts, model names and remote errors from executing ANSI/OSC codes.
    text = str(value)
    text = text[:limit] + ("..." if len(text) > limit else "")
    return json.dumps(text, ensure_ascii=True)[1:-1]


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _time(value: object) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, TypeError, OverflowError, OSError):
        return "unknown time"


def _describe_offline(record: dict, echo) -> None:
    decision = _mapping(record.get("decision"))
    offline = _mapping(record.get("offline"))
    if offline:
        echo(f"  Offline: {_display(offline.get('model'))} ({_display(offline.get('tier'))}), "
             f"score {_display(offline.get('score'))}/10; base {_display(offline.get('base_score', '?'))}, "
             f"learned {_display(offline.get('learned_adjustment', '?'))}")
        echo(f"    Classifier loaded: {_display(offline.get('classifier_loaded', 'unknown'))}; "
             f"thresholds: {_display(offline.get('thresholds', {}))}")
        if offline.get("signals"):
            echo(f"    Signals: {_display(offline['signals'], 600)}")
        if offline.get("matched_terms"):
            echo(f"    Learned terms: {_display(offline['matched_terms'], 600)}")
        if offline.get("caps"):
            echo(f"    Caps: {_display(offline['caps'])}")
    else:
        echo(f"  Offline: score {_display(decision.get('score', '?'))}/10; "
             "tier unavailable in this older log record")


def describe(record: dict, echo=print) -> None:
    decision = _mapping(record.get("decision"))
    jev = _mapping(record.get("jev"))
    old_answer = _mapping(decision.get("jev"))
    echo(f"{_time(record.get('timestamp'))}  {_display(record.get('request_id'))}")
    echo(f"  Origin: {_display(record.get('origin', 'unknown'))}")
    if isinstance(record.get("prompt"), str):
        echo(f"  Request: {_display(record['prompt'])}")
    _describe_offline(record, echo)
    if record.get("event") == "jev.request":
        echo(f"  Jev: evaluating... ({_display(jev.get('evaluation_model', record.get('model', 'unknown')))}; "
             f"{_display(jev.get('mode', record.get('mode', 'unknown')))} mode)")
        echo("  Final: pending")
        echo("")
        return
    choice = jev.get("tier", old_answer.get("choice"))
    status = jev.get("status", decision.get("reason", "unknown"))
    if choice:
        echo(f"  Jev: {_display(jev.get('model', choice))} ({_display(choice)}), "
             f"confidence {_display(jev.get('confidence', old_answer.get('confidence')))}; "
             f"{_display(status)}, {_display(record.get('latency_ms', '?'))} ms")
    else:
        echo(f"  Jev: {_display(status)}")
    agreement = record.get("agreement")
    compared = "agree" if agreement is True else "DISAGREE" if agreement is False else "not comparable"
    echo(f"  Final: {_display(record.get('final_model', decision.get('tier') or 'simple'))} "
         f"via {_display(decision.get('source', 'unknown'))} ({_display(decision.get('reason', 'unknown'))}); "
         f"evaluators {compared}")
    echo("")


def _key(record: dict) -> tuple | None:
    if record.get("event") in ("jev.request", *RESULT_EVENTS):
        return record["request_id"], record["event"]
    return None


def _version(path: Path) -> tuple | None:
    try:
        info = path.lstat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    except OSError:
        return None


def _watch(paths: tuple[Path, Path], records: list[dict], as_json: bool, request_id: str | None, echo) -> None:
    seen = {_key(r) for r in records}
    versions = None
    while True:
        current = tuple(_version(p) for p in paths)
        if current != versions:
            # Only reread on a change. Both reads and deduplication stay bounded by the two files.
            records = _read(paths[0]) + _read(paths[1])
            for record in records:
                key = _key(record)
                if key is not None and key not in seen and (request_id is None or record["request_id"] == request_id):
                    if as_json:
                        echo(json.dumps(record, ensure_ascii=True))
                    else:
                        describe(record, echo)
                    seen.add(key)
            seen = {_key(r) for r in records}
            versions = current
        time.sleep(0.1)


def render(path: Path, limit: int = 10, as_json: bool = False, request_id: str | None = None,
           echo=print, *, follow: bool = False) -> int:
    if not 1 <= limit <= 100:
        echo("--limit must be between 1 and 100")
        return 2
    if request_id is not None and not re.fullmatch(r"[a-f0-9]{32}", request_id):
        echo("--request-id must be the full 32-character ID shown in the log")
        return 2
    paths = (path.with_name(path.name + ".1"), path)
    records = _read(paths[0]) + _read(paths[1])
    results = [r for r in records if r.get("event") in RESULT_EVENTS
               and (request_id is None or r["request_id"] == request_id)][-limit:]
    completed = {r["request_id"] for r in records if r.get("event") in RESULT_EVENTS}
    pending = [r for r in records if follow and r.get("event") == "jev.request"
               and r["request_id"] not in completed and (request_id is None or r["request_id"] == request_id)]
    if not results and not follow:
        echo(f"No matching routing decisions in {path}. Routing may be off, or no request has been evaluated.")
        return 0
    if as_json:
        ids = {r["request_id"] for r in results + pending}
        for record in records:
            if record["request_id"] in ids:
                echo(json.dumps(record, ensure_ascii=True))
    else:
        for record in results + pending:
            describe(record, echo)
        if follow:
            echo(f"Watching {path} for evaluations. Press Ctrl+C to stop.")
        else:
            echo("Use --follow to watch live, --json for full records, and --request-id <id> for one request.")
    if follow:
        try:
            _watch(paths, records, as_json, request_id, echo)
        except KeyboardInterrupt:
            pass
    return 0
