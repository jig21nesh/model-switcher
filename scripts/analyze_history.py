"""Learn prompt-routing weights from what past prompts actually turned into.

The built-in scorer is a fixed keyword list. This reads local Claude Code transcripts and
asks a different question for every past prompt: did that prompt *become* real work? Tool
calls, file edits and spawned subagents are recorded in the transcript, so the answer is
observable rather than guessed, and the terms that predict it can be weighted.

Output is a portable JSON artifact, not Python. Any tool that can read JSON and split a
string into words can apply it; `docs/classifier-schema.md` specifies the format.

Nothing here runs on the interactive path. It is slow, it reads a lot of files, and it
never changes routing until you promote its candidate with --apply.
"""

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

try:  # hooks/ is a sibling package in the repo and a flat sibling file once installed.
    import complexity_router
except ModuleNotFoundError:  # pragma: no cover - exercised via the repo layout in tests
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    import complexity_router

SCHEMA_VERSION = 1
DEFAULT_TRANSCRIPTS = Path.home() / ".claude" / "projects"

# A term must look like a word someone would type, not an identifier or a secret.
TERM_RE = re.compile(r"^[a-z][a-z0-9-]{2,23}$")
# Long tokens containing digits are overwhelmingly keys, hashes and IDs rather than vocabulary.
SECRET_SHAPED_MIN_LENGTH = 12
# Multi-hyphen tokens are hostnames, branch names and paths pasted from terminal output rather
# than vocabulary. Dropping them removed a machine hostname from a real corpus at no accuracy cost.
MAX_HYPHENS = 1
# A term has to recur across genuinely separate sessions before it is worth weighting. This is
# also the privacy floor: something pasted once cannot reach the output file.
MIN_SESSION_SUPPORT = 3
# ...and it has to recur often enough that the ratio is not an accident. Without this, a term
# seen three times in heavy prompts and never elsewhere pins to the maximum weight.
MIN_OCCURRENCES = 10
# Shrink weights toward zero by how much evidence supports them: a term seen 10 times keeps
# a fifth of its raw log-odds, one seen 200 times keeps five sixths.
SHRINKAGE = 40
# Guard rails on the artifact so the scoring path stays cheap and bounded.
MAX_TERMS = 400
MAX_TERM_WEIGHT = 1.5
MAX_ADJUSTMENT = 3.0
MIN_TERM_WEIGHT = 0.35
# Below this there is not enough evidence for the weights to mean anything.
MIN_TRAINABLE_TURNS = 150
SMOOTHING = 0.5

# Low-signal common words. Without this the top of the table fills with "some", "want" and "will",
# which track an individual's phrasing rather than the work a prompt implies. Excluding them raised
# precision on a real corpus at no cost to recall.
#
# Two things to know if you edit this list. Terms are tokenized on non-alphanumerics, so an entry
# containing an apostrophe can never match anything — contractions appear here as the fragment that
# survives ("don", not "don't"). And it is deliberately not a pure function-word list: request-shaped
# verbs like "need", "ensure" and "confirm" measurably predict real work, so they are kept.
STOPWORDS = frozenset("""
about above after again all also always and another any anyone anything are aren around because
been before being below best better both but can couldn could did didn does doesn doing done don down
each end ended ends even ever every everyone everything few first for from further get got had has
hasn have haven having her here hers herself him himself his how into isn its itself just know knew
knows last let lets like look looked looks made make many may maybe might more most much must myself
never new next nor not nothing now off often okay old once one only onto other others ought our ours
out over own perhaps please quite rather really same saw see sees shall she should shouldn since
some someone something start started starts still such sure than thank thanks that the their theirs
them themselves then there these they thing things think thinks thought this three those through
time times too two under until use used uses very want wanted wants was wasn way ways were weren
what when where which while who whom whose why will with won would wouldn yeah yes yet you
your yours
""".split())

MUTATING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DELEGATING_TOOLS = {"Agent", "Task"}
# Thresholds for "this prompt became real work". Deliberately about actions taken, not words
# written: output tokens are inflated by thinking and say little about effort.
HEAVY_MUTATIONS = 3
HEAVY_TOOL_CALLS = 12
OBSERVABLE_MIN_OUTPUT_TOKENS = 200
CONTINUATION_MAX_WORDS = 12


def prompt_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(part for part in parts if isinstance(part, str))
    return ""


def iter_turns(transcript: Path):
    """Yield {text, session, tools, mutations, delegations, output, usage} per real user prompt.

    `usage` keeps the raw usage blocks the turn produced, keyed by message id so a streamed
    message counted once. Nothing here reads them; `tune` re-prices them at a different tier.
    """
    current = None
    try:
        handle = transcript.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("type")
            if kind == "user" and not record.get("isMeta") and not record.get("isSidechain"):
                text = prompt_text(record.get("message"))
                if not text.strip():
                    continue  # a tool_result turn is feedback to Claude, not a prompt
                if current:
                    yield current
                current = {
                    "text": text,
                    "session": str(record.get("sessionId") or transcript.stem),
                    "tools": 0, "mutations": 0, "delegations": 0, "output": 0, "usage": {},
                }
            elif kind == "assistant" and current is not None:
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if isinstance(usage, dict):
                    if isinstance(usage.get("output_tokens"), int):
                        current["output"] += usage["output_tokens"]
                    # Streaming rewrites the same message id and only the last entry carries final
                    # usage, so key on it exactly as cost_statusline.parse_transcript does.
                    key = message.get("id") or record.get("uuid") or f"entry-{len(current['usage'])}"
                    current["usage"][str(key)] = usage
                for block in message.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    current["tools"] += 1
                    name = block.get("name")
                    if name in MUTATING_TOOLS:
                        current["mutations"] += 1
                    if name in DELEGATING_TOOLS:
                        current["delegations"] += 1
    if current:
        yield current


def is_continuation(text: str) -> bool:
    """A short affirmation inherits work specified earlier and says nothing about itself."""
    stripped = text.strip().lower()
    return len(stripped.split()) <= CONTINUATION_MAX_WORDS and bool(
        complexity_router.AFFIRMATION_RE.match(stripped)
    )


def is_meta_prompt(text: str) -> bool:
    # Mirrors the router: slash commands and command echoes are never scored, so training on
    # them would teach weights that can never be applied.
    stripped = text.lstrip()
    return stripped.startswith("/") or bool(complexity_router.COMMAND_TAG_RE.search(text))


def is_observable(turn: dict) -> bool:
    return turn["tools"] > 0 or turn["output"] >= OBSERVABLE_MIN_OUTPUT_TOKENS


def is_heavy(turn: dict) -> bool:
    return (
        turn["delegations"] > 0
        or turn["mutations"] >= HEAVY_MUTATIONS
        or turn["tools"] >= HEAVY_TOOL_CALLS
    )


def _secret_shaped(term: str) -> bool:
    return len(term) >= SECRET_SHAPED_MIN_LENGTH and any(ch.isdigit() for ch in term)


def is_usable_term(token: str) -> bool:
    return (
        bool(TERM_RE.match(token))
        and token not in STOPWORDS
        and token.count("-") <= MAX_HYPHENS
        and not _secret_shaped(token)
    )


def terms_in(text: str) -> set[str]:
    """Distinct safe terms in a prompt. Never returns anything that was not already a plain word."""
    tokens = re.split(r"[^A-Za-z0-9-]+", text[: complexity_router.SCORE_MAX_CHARS].lower())
    return {token for token in (raw.strip("-") for raw in tokens) if is_usable_term(token)}


def trainable_turns(turns) -> list[dict]:
    kept = []
    for turn in turns:
        if is_continuation(turn["text"]) or is_meta_prompt(turn["text"]) or not is_observable(turn):
            continue
        turn["heavy"] = is_heavy(turn)
        kept.append(turn)
    return kept


def learn_weights(turns: list[dict]) -> dict[str, float]:
    """Log-odds of a term appearing in a prompt that became work, gated on session support."""
    heavy_hits: dict[str, int] = defaultdict(int)
    light_hits: dict[str, int] = defaultdict(int)
    sessions: dict[str, set] = defaultdict(set)
    heavy_total = light_total = 0

    for turn in turns:
        found = terms_in(turn["text"])
        if turn["heavy"]:
            heavy_total += 1
        else:
            light_total += 1
        for term in found:
            sessions[term].add(turn["session"])
            if turn["heavy"]:
                heavy_hits[term] += 1
            else:
                light_hits[term] += 1
    if not heavy_total or not light_total:
        return {}

    weights = {}
    for term, seen_in in sessions.items():
        occurrences = heavy_hits[term] + light_hits[term]
        if len(seen_in) < MIN_SESSION_SUPPORT or occurrences < MIN_OCCURRENCES:
            continue
        p_heavy = (heavy_hits[term] + SMOOTHING) / (heavy_total + 2 * SMOOTHING)
        p_light = (light_hits[term] + SMOOTHING) / (light_total + 2 * SMOOTHING)
        # Shrink toward zero by how much evidence there is, so a term seen ten times cannot
        # outweigh one seen two hundred times just because its ratio happens to be extreme.
        raw = math.log(p_heavy / p_light) * (occurrences / (occurrences + SHRINKAGE))
        weight = max(-MAX_TERM_WEIGHT, min(MAX_TERM_WEIGHT, raw))
        if abs(weight) >= MIN_TERM_WEIGHT:
            weights[term] = round(weight, 3)

    ranked = sorted(weights.items(), key=lambda item: -abs(item[1]))[:MAX_TERMS]
    return dict(sorted(ranked))


def learned_adjustment(text: str, weights: dict[str, float]) -> float:
    """Reference implementation of the artifact. Kept in step with the router in PR5."""
    total = sum(weights.get(term, 0.0) for term in terms_in(text))
    return max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, total))


def build_classifier(turns: list[dict], weights: dict[str, float], generated_at: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "generator": "model-switcher/analyze_history",
        "corpus": {
            "sessions": len({turn["session"] for turn in turns}),
            "prompts": len(turns),
            "heavy": sum(1 for turn in turns if turn["heavy"]),
            "light": sum(1 for turn in turns if not turn["heavy"]),
        },
        "scoring": {
            "max_adjustment": MAX_ADJUSTMENT,
            "min_session_support": MIN_SESSION_SUPPORT,
            "terms": weights,
        },
    }


def _accuracy(turns: list[dict], threshold: float, weights: dict[str, float] | None) -> dict:
    tp = fp = fn = tn = 0
    for turn in turns:
        score = complexity_router.score_prompt(turn["text"])
        if weights:
            score = max(0.0, min(10.0, score + learned_adjustment(turn["text"], weights)))
        routed = score >= threshold
        if routed and turn["heavy"]:
            tp += 1
        elif routed:
            fp += 1
        elif turn["heavy"]:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "false_positives": fp, "false_negatives": fn}


def report(classifier: dict, turns: list[dict], threshold: float) -> str:
    weights = classifier["scoring"]["terms"]
    corpus = classifier["corpus"]
    before = _accuracy(turns, threshold, None)
    after = _accuracy(turns, threshold, weights)

    lines = [
        "",
        f"corpus: {corpus['prompts']:,} usable prompts from {corpus['sessions']:,} sessions "
        f"({corpus['heavy']:,} became real work, {corpus['light']:,} did not)",
        f"learned: {len(weights)} terms, each seen in at least {MIN_SESSION_SUPPORT} sessions",
        "",
        f"routing accuracy at threshold {threshold:g}, measured on your own history:",
        f"{'':<14}{'precision':>11}{'recall':>9}{'F1':>7}{'wasted':>9}{'missed':>9}",
    ]
    for name, stats in (("built-in", before), ("with terms", after)):
        lines.append(
            f"  {name:<12}{stats['precision'] * 100:>10.1f}%{stats['recall'] * 100:>8.1f}%"
            f"{stats['f1'] * 100:>7.1f}{stats['false_positives']:>9,}{stats['false_negatives']:>9,}"
        )
    lines += [
        "",
        "  wasted = light prompts sent to an expensive model; missed = real work left on the cheap one",
        "",
    ]

    ranked = sorted(weights.items(), key=lambda item: -item[1])
    if ranked:
        lines.append("strongest signals that a prompt becomes real work:")
        lines.append("  " + ", ".join(f"{term} +{weight:.2f}" for term, weight in ranked[:12]))
        negatives = [pair for pair in ranked if pair[1] < 0][-12:]
        if negatives:
            lines.append("strongest signals that it does not:")
            lines.append("  " + ", ".join(f"{term} {weight:.2f}" for term, weight in reversed(negatives)))
    return "\n".join(lines)


def collect(transcript_dirs: list[Path], max_sessions: int | None) -> list[dict]:
    turns: list[dict] = []
    seen = 0
    for directory in transcript_dirs:
        if not directory.is_dir():
            continue
        for transcript in sorted(directory.glob("*/*.jsonl")) + sorted(directory.glob("*.jsonl")):
            if max_sessions is not None and seen >= max_sessions:
                return turns
            seen += 1
            turns.extend(iter_turns(transcript))
    return turns


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    # Derived from prompt text, so keep it readable only by its owner.
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True, help="model-switcher install directory")
    parser.add_argument(
        "--transcripts", type=Path, action="append", default=None,
        help=f"directory of Claude Code transcripts (default: {DEFAULT_TRANSCRIPTS})",
    )
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--threshold", type=float, default=complexity_router.DEFAULT_THRESHOLD)
    parser.add_argument("--apply", action="store_true", help="promote the candidate to the live classifier")
    parser.add_argument("--generated-at", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    directories = args.transcripts or [DEFAULT_TRANSCRIPTS]
    print("Reading local Claude Code transcripts to learn which prompts became real work.")
    for directory in directories:
        print(f"  scanning: {directory}")
    print("  nothing leaves this machine; no prompt text is written to the output.")

    turns = trainable_turns(collect(directories, args.max_sessions))
    if len(turns) < MIN_TRAINABLE_TURNS:
        print(
            f"\nonly {len(turns)} usable prompts found (need {MIN_TRAINABLE_TURNS}). "
            "Keep using Claude Code and run this again later.",
            file=sys.stderr,
        )
        return 1

    weights = learn_weights(turns)
    if not weights:
        print("\nno term met the evidence threshold; leaving the classifier alone.", file=sys.stderr)
        return 1

    classifier = build_classifier(turns, weights, args.generated_at)
    print(report(classifier, turns, args.threshold))

    candidate = args.home / "classifier.candidate.json"
    _write_json(candidate, classifier)
    if not args.apply:
        print(f"candidate written to {candidate}")
        print("Review it, then re-run with --apply to make it live. Routing is unchanged until you do.")
        return 0

    live = args.home / "classifier.json"
    _write_json(live, classifier)
    print(f"applied to {live} — it takes effect on your next prompt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
