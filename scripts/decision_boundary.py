"""How close a prompt was to routing differently, and what would have changed it.

A score and a verdict say what happened. They do not say whether it nearly went the other way,
which is the thing worth knowing when you are deciding whether to trust the routing or tune it.

Every claim here is derived rather than illustrated. Additions are measured by re-running the
real scorer on the prompt with the candidate word in it, so a suggestion can never name a signal
the router does not actually have. Removals are arithmetic on the parts analyse_prompt already
returned, through the router's own final_score(), so a counterfactual cannot disagree with what
the router would have done.
"""

from typing import NamedTuple

import complexity_router as router

# Enough to see the shape of the decision, few enough to read at a glance.
MAX_LISTED = 5
MAX_CANDIDATES = 3
LEARNED_CANDIDATES = 3


class Contribution(NamedTuple):
    """One thing that moved the score, and enough context to take it away again."""

    label: str
    value: float
    kind: str  # "signal" or "term"
    term: str | None = None


def edge_of_interest(score: int, config: dict) -> tuple[str, float]:
    """The threshold that decided this prompt: the one it crossed, or the nearest one it did not."""
    tier = router.select_tier(score, config)
    if tier is None:
        tier = "standard" if router.tiers_configured(config) == 3 else "complex"
    edge = router.threshold_from(config) if tier == "complex" else router.standard_threshold_from(config)
    return tier, edge


def contributions(detail: dict) -> list[Contribution]:
    """Everything that moved the score, largest first."""
    items = [Contribution(name, float(points), "signal") for name, points in detail["signals"]]
    items += [
        Contribution(f'learned term "{term}"', weight, "term", term)
        for term, weight in detail["matched_terms"].items()
    ]
    return sorted(items, key=lambda item: -abs(item.value))


def _clamped(total: float, classifier: dict) -> float:
    limit = classifier.get("max_adjustment", router.CLASSIFIER_MAX_ADJUSTMENT)
    return max(-limit, min(limit, total))


def score_without(detail: dict, classifier: dict, item: Contribution) -> int:
    """What this prompt would have scored without one contribution.

    The lookup caps are held as they were. Caps only ever lower a score, so this can overstate
    the counterfactual but never understate it: when it says the prompt would have dropped below
    a threshold, it would have.
    """
    base, adjustment = float(detail["base"]), float(detail["learned"])
    if item.kind == "signal":
        base -= item.value
    else:
        remaining = sum(w for term, w in detail["matched_terms"].items() if term != item.term)
        adjustment = _clamped(remaining, classifier)
    return router.final_score(base, adjustment, detail["caps"])


def _learned_candidates(detail: dict, classifier: dict) -> list[tuple[str, str]]:
    weights = classifier.get("terms") or {}
    unmatched = [
        (term, weight) for term, weight in weights.items()
        if weight > 0 and term not in detail["matched_terms"]
    ]
    unmatched.sort(key=lambda item: -item[1])
    return [(term, f'the learned term "{term}" ({weight:+.2f})')
            for term, weight in unmatched[:LEARNED_CANDIDATES]]


def _builtin_candidates(text: str) -> list[tuple[str, str]]:
    candidates = []
    for kind, patterns in (("task verb", router.STRONG_PATTERNS), ("domain term", router.MODERATE_PATTERNS)):
        for keyword, pattern in patterns:
            if not pattern.search(text):
                candidates.append((keyword, f'a built-in {kind}, e.g. "{keyword}"'))
                break
    return candidates


def flip_candidates(prompt: str, detail: dict, classifier: dict, config: dict) -> list[tuple[str, int, str | None]]:
    """Real signals that would raise this score, measured by scoring the prompt with each in it."""
    text = prompt[:router.SCORE_MAX_CHARS].lower()
    pool = _learned_candidates(detail, classifier) + _builtin_candidates(text)
    raised = []
    for word, description in pool:
        after = router.analyse_prompt(f"{prompt} {word}", classifier)
        if after["score"] > detail["score"]:
            raised.append((description, after["score"], router.select_tier(after["score"], config)))
    raised.sort(key=lambda item: -item[1])
    return raised[:MAX_CANDIDATES]


def _points(count: float) -> str:
    return f"{count:g} point" + ("" if count == 1 else "s")


def _destination(tier: str | None, config: dict) -> str:
    if tier is None:
        return "answered in-session"
    model = config.get("models", {}).get(tier) if isinstance(config.get("models"), dict) else None
    return f"{router.TIER_LABELS[tier]} -> {router.agent_name_for(model, tier)}" if model \
        else router.TIER_LABELS[tier]


def _mark(term: str | None, topical) -> str:
    return "   topical: seen in only one project" if term in topical else ""


def _render_carried(detail: dict, classifier: dict, config: dict, topical, echo) -> bool:
    items = contributions(detail)
    shown = items[:MAX_LISTED]
    echo("    what carried it there")
    for item in shown:
        echo(f"      {item.label:<44}{item.value:>+7.2f}{_mark(item.term, topical)}")
    if len(items) > MAX_LISTED:
        echo(f"      ... and {len(items) - MAX_LISTED} more")

    largest = next((item for item in items if item.value > 0), None)
    if largest is not None:
        without = score_without(detail, classifier, largest)
        landing = _destination(router.select_tier(without, config), config)
        echo(f"    without {largest.label} ({largest.value:+g}) it scores {without} — {landing}")
    return any(item.term in topical for item in shown)


def _render_flip(prompt: str, detail: dict, classifier: dict, config: dict, echo) -> None:
    candidates = flip_candidates(prompt, detail, classifier, config)
    echo("    what would flip it")
    if not candidates:
        echo("      nothing in the built-in table or your learned terms raises this score")
        return
    # Signals that actually change the destination come first; a raise that lands in the same
    # place is context, not a flip, and must not be allowed to look like one.
    current = router.select_tier(detail["score"], config)
    for description, score, tier in sorted(candidates, key=lambda item: (item[2] == current, -item[1])):
        echo(f"      + {description:<48}scores {score}   {_destination(tier, config)}")
    if all(tier == current for _, _, tier in candidates):
        echo("      none of these cross the edge on their own")


def _render_holding_back(detail: dict, classifier: dict, topical, echo) -> bool:
    lines = []
    if detail["caps"]:
        uncapped = router.final_score(detail["base"], detail["learned"], [])
        lines.append((f"capped to {router.CAPPED_SCORE} by {', '.join(detail['caps'])} — the cap is applied "
                      f"after learned weights, so no term can lift this (uncapped it would score {uncapped})"))
    negatives = sorted(
        (item for item in contributions(detail) if item.value < 0), key=lambda item: item.value
    )
    shown = negatives[:MAX_LISTED]
    for item in shown:
        lines.append(f"{item.label:<44}{item.value:>+7.2f}{_mark(item.term, topical)}")
    if not detail["signals"]:
        lines.append("no built-in signal matched, so the score started at 0")
    if not lines:
        return False
    echo("    what is holding it back")
    for line in lines:
        echo(f"      {line}")
    return any(item.term in topical for item in shown)


def render(prompt: str, detail: dict, config: dict, classifier: dict, topical=frozenset(), echo=print) -> None:
    """The sensitivity block that follows the verdict in `explain`."""
    score = detail["score"]
    tier, edge = edge_of_interest(score, config)
    routed = router.select_tier(score, config) is not None
    gap = score - edge

    echo("  decision boundary")
    if gap == 0:
        echo(f"    exactly on the {router.TIER_LABELS[tier]} threshold ({edge:g}) — "
             "one point either way changes the verdict")
    elif routed:
        echo(f"    {_points(gap)} clear of the {router.TIER_LABELS[tier]} threshold ({edge:g})")
    else:
        echo(f"    {_points(-gap)} short of the {router.TIER_LABELS[tier]} threshold ({edge:g}), "
             "the nearest routing edge")

    marked = False
    if routed:
        marked = _render_carried(detail, classifier, config, topical, echo)
    else:
        _render_flip(prompt, detail, classifier, config, echo)
    # Only footnote a mark that was actually printed: a topical term the lists did not reach
    # would leave the reader looking for something that is not there.
    if _render_holding_back(detail, classifier, topical, echo) or marked:
        echo("    topical terms come from a sample of your transcripts — "
             "'model-switcher classifier' does the full pass")
