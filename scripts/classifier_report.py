"""The `classifier` report: what the learned artifact contains, and where its vocabulary came from.

`learn` prints an accuracy comparison at the moment it writes the file and `status` prints one
line about it. Neither answers the question that decides whether the weights are worth keeping:
what is actually in there, and is it measuring difficulty or just measuring which project you
happened to be working in.

Per-project attribution is the point of this report. Transcripts are stored one directory per
project, so every learned term can be traced back to the projects whose prompts contained it. A
term that only ever appears in one project is topic vocabulary: it tells the router which repo
you are in, not how hard the prompt is, and it will mis-score the day you start something else.

Both inputs are untrusted. The artifact and the transcripts are parsed with stdlib json only;
terms are compared as plain strings and never compiled, and no prompt text is printed or stored.
"""

import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

import analyze_history
import complexity_router as router

# Below this a weight is noise dressed up as evidence: it takes six such terms to move a score
# by a single point, and a score only ever routes on whole points.
WEAK_WEIGHT = 0.5
# The producer clamps each weight to +-MAX_TERM_WEIGHT. A weight this close to the clamp was cut
# off by it rather than measured by it, so the true separation is unknown.
NEAR_CLAMP = 0.9 * analyze_history.MAX_TERM_WEIGHT
# Two terms landing on the same weight is coincidence; this many is the evidence floor showing
# through — every one of them was seen the minimum number of times and nothing more.
MIN_TIE_CLUSTER = 3
# A term that takes this share of its prompts from one project is that project's vocabulary,
# whatever the odd appearance elsewhere suggests.
CONCENTRATED_SHARE = 0.9
TOP_TERMS = 10
TOP_PROJECTS = 12
TOP_SINGLE_PROJECT = 30
BAR_WIDTH = 22
# Long enough to identify a project, short enough to keep the table readable.
PROJECT_LABEL_CHARS = 40


def _is_number(value: object) -> bool:
    # Mirrors complexity_router._is_number: finite only — NaN/Infinity parse as JSON but fall
    # outside every distribution band and poison the weight range line.
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def read_artifact(path: Path) -> tuple[dict | None, str]:
    """The artifact as the router would see it, or the reason it is unusable."""
    try:
        if path.stat().st_size > router.CLASSIFIER_MAX_BYTES:
            return None, f"{path} is larger than {router.CLASSIFIER_MAX_BYTES} bytes — the router ignores it"
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, (f"no classifier at {path}\n"
                      "learn one from your own history with: model-switcher learn --apply")
    except (OSError, ValueError) as exc:
        return None, f"cannot read {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path} is not a JSON object — the router ignores it"
    version = data.get("schema_version")
    if version != router.CLASSIFIER_SCHEMA_VERSION:
        return None, (f"{path} declares schema_version {str(version)[:32]!r}, not "
                      f"{router.CLASSIFIER_SCHEMA_VERSION} — the router ignores it")
    return data, ""


def usable_weights(data: dict) -> tuple[dict[str, float], int, int]:
    """Split the term table the way the router does: kept, rejected, and past its term limit."""
    scoring = data.get("scoring")
    terms = scoring.get("terms") if isinstance(scoring, dict) else None
    if not isinstance(terms, dict):
        return {}, 0, 0

    weights: dict[str, float] = {}
    invalid = 0
    for index, (term, weight) in enumerate(terms.items()):
        if len(weights) >= router.CLASSIFIER_MAX_TERMS:
            # The router stops reading here, so everything after this is not in effect either.
            return weights, invalid, len(terms) - index
        if isinstance(term, str) and router.CLASSIFIER_TERM_RE.match(term) and _is_number(weight):
            weights[term] = float(weight)
        else:
            invalid += 1
    return weights, invalid, 0


def max_adjustment_of(data: dict) -> float:
    scoring = data.get("scoring")
    limit = scoring.get("max_adjustment") if isinstance(scoring, dict) else None
    if not _is_number(limit) or not 0 < limit <= router.CLASSIFIER_MAX_ADJUSTMENT:
        return router.CLASSIFIER_MAX_ADJUSTMENT
    return float(limit)


def distribution(weights: dict[str, float]) -> list[tuple[str, str, int]]:
    """How the weights are spread, as (band, what it means, count)."""
    bands = (
        (0.0, WEAK_WEIGHT, "weak — six of these move a score by one point"),
        (WEAK_WEIGHT, 1.0, "moves a score on its own"),
        (1.0, NEAR_CLAMP, "strong"),
        (NEAR_CLAMP, float("inf"), f"at or near the +-{analyze_history.MAX_TERM_WEIGHT:g} clamp, so cut off"),
    )
    rows = []
    for low, high, meaning in bands:
        count = sum(1 for weight in weights.values() if low <= abs(weight) < high)
        label = f"|w| < {high:g}" if low == 0 else (
            f"|w| >= {low:g}" if high == float("inf") else f"{low:g} <= |w| < {high:g}"
        )
        rows.append((label, meaning, count))
    return rows


def tie_clusters(weights: dict[str, float]) -> list[tuple[float, list[str]]]:
    """Terms sharing one exact weight, largest group first. Ties carry no ranking information."""
    grouped: dict[float, list[str]] = defaultdict(list)
    for term, weight in weights.items():
        grouped[weight].append(term)
    clusters = [(weight, sorted(terms)) for weight, terms in grouped.items() if len(terms) >= MIN_TIE_CLUSTER]
    return sorted(clusters, key=lambda item: (-len(item[1]), item[0]))


def _prompts_in(transcript: Path):
    """The prompts of a transcript that `learn` would have trained on, and nothing else."""
    for turn in analyze_history.iter_turns(transcript):
        text = turn["text"]
        if analyze_history.is_continuation(text) or analyze_history.is_meta_prompt(text):
            continue
        if not analyze_history.is_observable(turn):
            continue
        yield text


def _project_files(root: Path, per_project_limit: int | None) -> list[tuple[str, list[Path]]]:
    projects = []
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        files = list(directory.glob("*.jsonl"))
        if per_project_limit is not None:
            # Newest first, so a bounded sample is the work you have actually been doing.
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            files = files[:per_project_limit]
        else:
            files.sort()
        if files:
            projects.append((directory.name, files))
    return projects


def attribute(
    terms, root: Path, per_project_limit: int | None = None, stop_at: int | None = None
) -> dict:
    """How many prompts in each project contained each term.

    `per_project_limit` samples the newest transcripts of each project instead of reading all of
    them, and `stop_at` stops tracking a term once it has turned up in that many projects. Both
    exist for `explain`, which only needs to know whether a term is confined to one project and
    must not spend a full corpus pass finding out. Either one makes the per-project counts a
    sample rather than a total; the report passes neither.
    """
    result = {
        "prompts_of": {term: Counter() for term in terms},
        "by_project": {},
        "projects": 0,
        "files": 0,
        "prompts": 0,
        "sampled": per_project_limit is not None or stop_at is not None,
    }
    if not root.is_dir():
        result["missing"] = True
        return result

    try:
        projects = _project_files(root, per_project_limit)
    except OSError:
        result["missing"] = True
        return result

    wanted = set(result["prompts_of"])
    for name, files in projects:
        result["projects"] += 1
        for transcript in files:
            result["files"] += 1
            for text in _prompts_in(transcript):
                result["prompts"] += 1
                for term in router.classifier_terms(text) & wanted:
                    result["prompts_of"][term][name] += 1
        if stop_at is not None:
            wanted = {term for term in wanted if len(result["prompts_of"][term]) < stop_at}
            if not wanted:
                break

    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"terms": 0, "only": 0})
    for term, seen in result["prompts_of"].items():
        for name in seen:
            counts[name]["terms"] += 1
            if len(seen) == 1:
                counts[name]["only"] += 1
    result["by_project"] = dict(counts)
    return result


def single_project_terms(attribution: dict) -> list[str]:
    """Terms whose every prompt came from one project. Topic vocabulary, not difficulty."""
    return sorted(term for term, seen in attribution["prompts_of"].items() if len(seen) == 1)


def concentrated_terms(attribution: dict, share: float = CONCENTRATED_SHARE) -> list[tuple[str, float]]:
    """Terms that spread to a second project but still take almost all their prompts from one.

    A term used ten times in one repo and once in another is that repo's vocabulary with a
    rounding error attached, and it will mis-score everywhere else exactly as a single-project
    term does. Reported separately so the stricter claim stays strictly true.
    """
    found = []
    for term, seen in attribution["prompts_of"].items():
        total = sum(seen.values())
        if len(seen) > 1 and total and max(seen.values()) / total >= share:
            found.append((term, max(seen.values()) / total))
    return sorted(found, key=lambda item: -item[1])


def _count(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else "?"


def _text(value: object, fallback: str = "unknown") -> str:
    # Metadata is untrusted: show it, but never let a long or odd value reshape the report.
    return str(value)[:60].replace("\n", " ") if isinstance(value, (str, int, float)) else fallback


def _label(name: str) -> str:
    return name if len(name) <= PROJECT_LABEL_CHARS else "..." + name[-(PROJECT_LABEL_CHARS - 3):]


def _terms_line(pairs) -> str:
    return ", ".join(f"{term} {weight:+.2f}" for term, weight in pairs)


def render_provenance(path: Path, data: dict, weights: dict, ignored: tuple[int, int], echo=print) -> None:
    corpus = data.get("corpus") if isinstance(data.get("corpus"), dict) else {}
    heavy, light = corpus.get("heavy"), corpus.get("light")
    split = "heavy/light split not recorded"
    if isinstance(heavy, int) and isinstance(light, int) and heavy + light:
        split = (f"{heavy:,} became real work, {light:,} did not "
                 f"({100.0 * heavy / (heavy + light):.0f}% heavy)")

    echo(f"classifier   {path}")
    echo("")
    echo(f"  learned      {_text(data.get('generated_at'))}")
    echo(f"  generator    {_text(data.get('generator'))}, schema version {data.get('schema_version')}")
    echo(f"  corpus       {_count(corpus.get('prompts'))} prompts from "
         f"{_count(corpus.get('sessions'))} sessions")
    echo(f"               {split}")

    if weights:
        low, high = min(weights.values()), max(weights.values())
        echo(f"  terms        {len(weights):,} in effect, weights {low:+.2f} to {high:+.2f}, "
             f"bounded to +-{max_adjustment_of(data):g} per prompt")
    else:
        echo("  terms        none in effect")
    invalid, over_limit = ignored
    if invalid:
        echo(f"               {invalid:,} entries the router rejects (not a usable term or weight)")
    if over_limit:
        echo(f"               {over_limit:,} entries past the router's {router.CLASSIFIER_MAX_TERMS:,}-term "
             "limit, which it never reads")
    echo("")


def render_distribution(weights: dict[str, float], echo=print) -> None:
    total = len(weights)
    echo("  weight distribution")
    for label, meaning, count in distribution(weights):
        share = 100.0 * count / total
        bar = "#" * round(share * BAR_WIDTH / 100)
        echo(f"    {label:<20}{count:>5}{share:>5.0f}%  {bar:<{BAR_WIDTH}}  {meaning}")
    positive = sum(1 for weight in weights.values() if weight > 0)
    echo(f"    {'':<20}{'':>5}{'':>5}  {'':<{BAR_WIDTH}}  "
         f"{positive} positive, {total - positive} negative")
    echo("")

    clusters = tie_clusters(weights)
    if clusters:
        weight, terms = clusters[0]
        echo(f"  evidence floor  {len(terms)} terms share exactly {weight:+.3f}")
        echo("                  a weight that many terms agree on is the minimum-evidence floor showing")
        echo("                  through, not a measurement: they are indistinguishable from each other")
        extra = ", ".join(f"{len(group)} at {value:+.3f}" for value, group in clusters[1:4])
        if extra:
            echo(f"                  also {extra}")
        echo("")


def render_strongest(weights: dict[str, float], echo=print) -> None:
    ranked = sorted(weights.items(), key=lambda item: -item[1])
    positive = [pair for pair in ranked if pair[1] > 0][:TOP_TERMS]
    negative = [pair for pair in reversed(ranked) if pair[1] < 0][:TOP_TERMS]
    if positive:
        echo("  strongest evidence a prompt becomes real work")
        echo(f"    {_terms_line(positive)}")
    if negative:
        echo("  strongest evidence it does not")
        echo(f"    {_terms_line(negative)}")
    echo("")


def render_attribution(weights: dict[str, float], attribution: dict, echo=print) -> None:
    if attribution.get("missing"):
        echo("  vocabulary by project   skipped: no transcripts directory to read")
        echo("")
        return

    echo(f"  vocabulary by project   {attribution['projects']} projects, "
         f"{attribution['files']:,} transcripts, {attribution['prompts']:,} prompts read")
    ranked = sorted(attribution["by_project"].items(), key=lambda item: (-item[1]["terms"], item[0]))
    if ranked:
        echo(f"    {'project':<{PROJECT_LABEL_CHARS}}{'terms':>7}{'only here':>11}")
        for name, counts in ranked[:TOP_PROJECTS]:
            echo(f"    {_label(name):<{PROJECT_LABEL_CHARS}}{counts['terms']:>7}{counts['only']:>11}")
        if len(ranked) > TOP_PROJECTS:
            echo(f"    ... and {len(ranked) - TOP_PROJECTS} more projects")
    unattributed = sum(1 for seen in attribution["prompts_of"].values() if not seen)
    if unattributed:
        echo(f"    {unattributed} terms matched no prompt in these transcripts")
    echo("")

    confined = single_project_terms(attribution)
    concentrated = concentrated_terms(attribution)
    if not confined and not concentrated:
        echo("  no term is tied to a single project — this vocabulary is about the work, not the repo")
        echo("")
        return

    echo("  topic, not difficulty")
    echo("    a term whose prompts all come from one project measures which project you are in, not")
    echo("    how hard the prompt is; it will mis-score the day you start working somewhere else")
    if confined:
        echo(f"    {'one project only':<22}{len(confined)} of {len(weights)} terms "
             f"({100.0 * len(confined) / len(weights):.0f}%)")
        _echo_terms(confined, weights, echo)
    if concentrated:
        echo(f"    {f'{CONCENTRATED_SHARE:.0%}+ from one project':<22}{len(concentrated)} more terms")
        _echo_terms([term for term, _ in concentrated], weights, echo)
    echo("")


def _echo_terms(terms: list[str], weights: dict[str, float], echo) -> None:
    listed = sorted(((term, weights[term]) for term in terms), key=lambda item: -abs(item[1]))
    echo(f"      {_terms_line(listed[:TOP_SINGLE_PROJECT])}")
    if len(listed) > TOP_SINGLE_PROJECT:
        echo(f"      ... and {len(listed) - TOP_SINGLE_PROJECT} more")


def render(path: Path, transcripts: Path, echo=print) -> int:
    """Print the whole report. Returns the exit code: non-zero when nothing is in effect."""
    data, error = read_artifact(path)
    if data is None:
        print(error, file=sys.stderr)
        return 2

    weights, invalid, over_limit = usable_weights(data)
    render_provenance(path, data, weights, (invalid, over_limit), echo)
    if not weights:
        echo("  nothing here is in effect — routing runs on the built-in signals alone")
        echo("  learn a usable table from your own history with: model-switcher learn --apply")
        return 2

    render_distribution(weights, echo)
    render_strongest(weights, echo)
    render_attribution(weights, attribute(set(weights), transcripts), echo)
    return 0
