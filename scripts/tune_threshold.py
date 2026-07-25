"""The `tune` report: evidence for complexity.threshold, measured on your own history.

`complexity.threshold` ships as 5 because a number was needed, not because 5 was measured. This
command asks what a corpus actually says: at each score the router produces, how often did a
prompt at that score become real work, and what would each candidate threshold have delegated,
caught, wasted and cost.

The labelling is `learn`'s, unchanged (analyze_history.trainable_turns): a prompt "became real
work" when it spawned a subagent, made three or more edits, or ran twelve or more tool calls.
Nothing here re-derives it, so the two commands cannot disagree about what a heavy prompt is.
Scoring likewise goes through the router, learned classifier included, so the table describes the
routing that is actually installed rather than a built-in-only approximation of it.

The cost column is a counterfactual and is labelled as one wherever it appears: it re-prices each
prompt's *own recorded tokens* at the tier it would have routed to, which assumes the other tier
would have burned the same tokens. It would not. See ADR-0010.

Transcript content is untrusted: read with stdlib json via analyze_history, never printed, never
written anywhere. Only counts and rates reach the output.
"""

import math

import analyze_history
import complexity_router as router
import cost_statusline as statusline

MAX_SCORE = 10
# Below 3 nearly everything delegates and above 8 nearly nothing does; both ends are degenerate
# rather than informative. The user's own threshold is always added to this list.
CANDIDATE_THRESHOLDS = (3, 4, 5, 6, 7, 8)
BAR_WIDTH = 20
# An F1 difference smaller than this is not worth telling someone to edit their config over: on a
# few hundred prompts it is comfortably inside the noise of which sessions happened to be scanned.
MIN_F1_GAIN = 0.02
COST_BASIS_PROMPTS = 1000


def collect(directories: list, max_sessions: int | None) -> list[dict]:
    """Usable, labelled prompts from the transcript corpus — exactly what `learn` trains on."""
    return analyze_history.trainable_turns(analyze_history.collect(directories, max_sessions))


def score_turns(turns: list[dict], classifier: dict) -> None:
    for turn in turns:
        turn["score"] = router.score_prompt(turn["text"], classifier)


def calibration(turns: list[dict]) -> list[dict]:
    """For every score the router can emit: how many prompts landed there, and how many were work."""
    rows = [{"score": score, "prompts": 0, "heavy": 0, "rate": 0.0} for score in range(MAX_SCORE + 1)]
    for turn in turns:
        row = rows[turn["score"]]
        row["prompts"] += 1
        row["heavy"] += 1 if turn["heavy"] else 0
    for row in rows:
        if row["prompts"]:
            row["rate"] = row["heavy"] / row["prompts"]
    return rows


def tier_rates(config: dict) -> tuple[dict, str]:
    """Rates for the two tiers a prompt can land on, or an empty dict and the reason there are none.

    Guessing a rate would make the cost column look authoritative while being invented, so a
    missing or unpriced model drops the column instead.
    """
    pricing = statusline.usable_pricing(config)
    if not pricing:
        return {}, "no usable rates in pricing_usd_per_mtok — run 'model-switcher pricing --yes'"
    models = config.get("models")
    models = models if isinstance(models, dict) else {}
    resolved = {}
    for tier in ("simple", "complex"):
        name = models.get(tier)
        if not isinstance(name, str) or not name.strip():
            return {}, f"models.{tier} is not set, so the two tiers cannot be priced against each other"
        key = router.resolve_alias(name, pricing)
        if key is None:
            return {}, f"models.{tier} '{name}' has no entry in pricing_usd_per_mtok"
        resolved[tier] = pricing[key]
    return resolved, ""


def price_turns(turns: list[dict], rates: dict) -> bool:
    """Attach what each prompt's own tokens cost on each tier. False when nothing is priceable.

    Transcripts written before usage was recorded, or scanned from a corpus of empty sessions,
    produce a table of zeroes that reads like a real answer. Report that there is nothing to
    re-price instead.
    """
    billable = 0
    for turn in turns:
        usages = list((turn.get("usage") or {}).values())
        billable += sum(statusline.billable_tokens(usage) for usage in usages)
        for tier, tier_rates_ in rates.items():
            turn[f"cost_{tier}"] = sum(
                statusline.usage_cost(usage, statusline.effective_rates(usage, tier_rates_))
                for usage in usages
            )
    return billable > 0


def candidates_for(threshold: float) -> list[float]:
    # The user's own threshold always appears, even when it is not one of the round numbers, so
    # both tables have a row to mark and the recommendation has something to compare against.
    return sorted(set(CANDIDATE_THRESHOLDS) | {threshold})


def sweep(turns: list[dict], candidates: list, priced: bool) -> list[dict]:
    """What each candidate threshold would have done to this corpus."""
    total = len(turns)
    rows = []
    for threshold in candidates:
        tp = fp = fn = 0
        cost = 0.0
        for turn in turns:
            delegates = turn["score"] >= threshold
            if delegates and turn["heavy"]:
                tp += 1
            elif delegates:
                fp += 1
            elif turn["heavy"]:
                fn += 1
            if priced:
                cost += turn["cost_complex"] if delegates else turn["cost_simple"]
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        rows.append({
            "threshold": threshold,
            "delegated": (tp + fp) / total,
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
            "cost": cost / total * COST_BASIS_PROMPTS if priced else None,
        })
    return rows


def recommend(rows: list[dict], threshold: float, turns: list[dict]) -> str:
    """One line, and only when the corpus supports one.

    Inventing a suggestion from a handful of prompts is worse than saying nothing: the user would
    edit a config on the strength of it.
    """
    total = len(turns)
    heavy = sum(1 for turn in turns if turn["heavy"])
    if total < analyze_history.MIN_TRAINABLE_TURNS:
        return (
            f"no recommendation: {total:,} usable prompts is too few to conclude anything "
            f"(want at least {analyze_history.MIN_TRAINABLE_TURNS}). The table above is still what "
            "your corpus says — just not yet enough of it."
        )
    if not heavy or heavy == total:
        return (
            "no recommendation: every prompt in this corpus carries the same label, so precision "
            "and recall are degenerate here and no threshold can be told apart from another."
        )

    current = next(row for row in rows if row["threshold"] == threshold)
    # Ties resolve toward the threshold already in use: an equal-scoring change is not a change
    # worth making.
    best = max(rows, key=lambda row: (row["f1"], -abs(row["threshold"] - threshold)))
    if best["threshold"] == threshold:
        return f"recommendation: keep threshold {threshold:g} — it scores best of the candidates on your history."
    if best["f1"] - current["f1"] < MIN_F1_GAIN:
        return (
            f"no recommendation: nothing beats your current {threshold:g} by enough to matter "
            f"(best is {best['threshold']:g} at F1 {best['f1'] * 100:.1f} vs {current['f1'] * 100:.1f}). "
            "Leave it alone."
        )
    # F1 is blind to which error is dearer, so a recommendation that moves the delegated share
    # states its price. Recommending "delegate more" without it would hide the whole trade.
    price = ""
    if current["cost"] is not None:
        price = (f", est. {statusline.format_cost(best['cost'])} vs "
                 f"{statusline.format_cost(current['cost'])} per {COST_BASIS_PROMPTS:,} prompts")
    return (
        f"recommendation: try threshold {best['threshold']:g} — F1 {best['f1'] * 100:.1f} vs "
        f"{current['f1'] * 100:.1f} at {threshold:g}, delegating {best['delegated'] * 100:.0f}% of "
        f"prompts instead of {current['delegated'] * 100:.0f}%{price}."
    )


def render_calibration(rows: list[dict], threshold: float, echo=print) -> None:
    # Scores are integers, so the first score that routes is the ceiling of the threshold.
    routes_from = math.ceil(threshold)
    echo("what prompts at each score actually did:")
    echo("")
    echo(f"  {'score':>5}{'prompts':>10}   became real work")
    for row in rows:
        bar = "#" * round(row["rate"] * BAR_WIDTH) if row["prompts"] else ""
        share = f"{row['rate'] * 100:.0f}%" if row["prompts"] else "-"
        marker = f"   <- current threshold ({threshold:g})" if row["score"] == routes_from else ""
        echo(f"  {row['score']:>5}{row['prompts']:>10,}   {bar:<{BAR_WIDTH}} {share:>4}{marker}")
    echo("")
    echo("  'real work' is the label 'learn' uses: the prompt spawned a subagent, made 3+ edits,")
    echo("  or ran 12+ tool calls. Prompts at or above the marked score are delegated today.")


def render_sweep(rows: list[dict], threshold: float, priced: bool, why_not: str, echo=print) -> None:
    echo("what each candidate threshold would do:")
    echo("")
    header = f"  {'threshold':>9}{'delegated':>11}{'precision':>11}{'recall':>9}{'F1':>7}"
    echo(header + (f"{'$/1k prompts':>15}" if priced else ""))
    for row in rows:
        line = (
            f"  {row['threshold']:>9g}{row['delegated'] * 100:>10.1f}%{row['precision'] * 100:>10.1f}%"
            f"{row['recall'] * 100:>8.1f}%{row['f1'] * 100:>7.1f}"
        )
        if priced:
            line += f"{statusline.format_cost(row['cost']):>15}"
        if row["threshold"] == threshold:
            line += "   <- current"
        echo(line)
    echo("")
    echo("  delegated = share of prompts sent to models.complex. precision = of those, the share")
    echo("  that were real work. recall = of all the real work, the share that got there.")
    if priced:
        echo("")
        echo("  $/1k prompts is an ESTIMATE, not a quote: it re-prices each prompt's own recorded")
        echo("  tokens at the tier it would route to, which assumes the same prompt burns the same")
        echo("  tokens on either model. It does not — a stronger model may finish in fewer turns, or")
        echo("  think for longer. Read it as a direction of travel, not a bill.")
    else:
        echo("")
        echo(f"  cost column omitted: {why_not}")


def report(config: dict, turns: list[dict], echo=print) -> None:
    """Print both tables and, if the corpus earns it, a recommendation."""
    classifier = router.load_classifier()
    score_turns(turns, classifier)
    threshold = router.threshold_from(config)
    rates, why_not = tier_rates(config)
    priced = bool(rates) and price_turns(turns, rates)
    if rates and not priced:
        why_not = "these transcripts record no token usage, so there is nothing to re-price"

    heavy = sum(1 for turn in turns if turn["heavy"])
    sessions = len({turn["session"] for turn in turns})
    terms = len(classifier.get("terms") or {})
    scored_with = f"built-in signals + {terms} learned terms" if terms else "built-in signals only"
    echo("")
    echo(f"corpus: {len(turns):,} usable prompts from {sessions:,} sessions "
         f"({heavy:,} became real work, {len(turns) - heavy:,} did not)")
    echo(f"scored: {scored_with}")
    echo("")
    render_calibration(calibration(turns), threshold, echo)
    echo("")
    rows = sweep(turns, candidates_for(threshold), priced)
    render_sweep(rows, threshold, priced, why_not, echo)
    echo("")
    echo(recommend(rows, threshold, turns))
    if router.tiers_configured(config) == 3:
        echo("(a middle tier is configured; this sweep models in-session vs models.complex only.)")
