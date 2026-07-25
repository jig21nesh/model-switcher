import pytest

import complexity_router as router
import decision_boundary as boundary

TWO_TIER = {"models": {"complex": "fable", "simple": "sonnet"}, "complexity": {"threshold": 5}}
THREE_TIER = {
    "models": {"complex": "fable", "standard": "sonnet", "simple": "haiku"},
    "complexity": {"threshold": 5, "standard_threshold": 3},
}


def classifier(terms, limit=3.0):
    return {"terms": terms, "max_adjustment": limit}


def analyse(prompt, terms=None):
    weights = classifier(terms) if terms else {}
    return prompt, router.analyse_prompt(prompt, weights), weights


def render(prompt, config=TWO_TIER, terms=None, topical=frozenset()):
    lines = []
    prompt, detail, weights = analyse(prompt, terms)
    boundary.render(prompt, detail, config, weights, topical, echo=lines.append)
    return "\n".join(lines)


class TestEdgeOfInterest:
    def test_a_routed_prompt_reports_the_threshold_it_crossed(self):
        assert boundary.edge_of_interest(9, TWO_TIER) == ("complex", 5.0)

    def test_a_middle_tier_prompt_reports_its_own_edge(self):
        assert boundary.edge_of_interest(4, THREE_TIER) == ("standard", 3.0)

    def test_an_in_session_prompt_reports_the_nearest_edge_above_it(self):
        assert boundary.edge_of_interest(1, THREE_TIER) == ("standard", 3.0)
        assert boundary.edge_of_interest(1, TWO_TIER) == ("complex", 5.0)


class TestContributions:
    def test_orders_signals_and_terms_by_size(self):
        _, detail, _ = analyse("refactor the api schema", {"schema": 1.4})
        labels = [item.label for item in boundary.contributions(detail)]
        assert labels[0].startswith("task verbs")
        assert 'learned term "schema"' in labels

    def test_a_prompt_with_nothing_in_it_contributes_nothing(self):
        _, detail, _ = analyse("hmm")
        assert boundary.contributions(detail) == []


class TestScoreWithout:
    def test_removing_a_signal_matches_what_the_router_would_have_scored(self):
        _, detail, weights = analyse("refactor the auth module and migrate the schema")
        signal = next(item for item in boundary.contributions(detail) if item.kind == "signal")
        assert boundary.score_without(detail, weights, signal) == detail["score"] - signal.value

    def test_removing_a_term_recomputes_the_learned_adjustment(self):
        _, detail, weights = analyse("ensure the pipeline works", {"ensure": 1.4, "pipeline": 0.6})
        term = next(item for item in boundary.contributions(detail) if item.term == "ensure")
        without = boundary.score_without(detail, weights, term)
        assert without == router.analyse_prompt("the pipeline works", weights)["score"]

    def test_the_clamp_still_applies_after_a_term_is_removed(self):
        terms = {"ensure": 1.5, "pipeline": 1.5, "deploy": 1.5}
        _, detail, weights = analyse("ensure the pipeline can deploy", terms)
        term = next(item for item in boundary.contributions(detail) if item.term == "ensure")
        assert detail["learned"] == 3.0, "the sum was clamped"
        assert boundary.score_without(detail, weights, term) == detail["score"], "still clamped"

    def test_a_capped_prompt_stays_capped(self):
        _, detail, weights = analyse("what is a mutex?", {"mutex": 1.5})
        term = next(item for item in boundary.contributions(detail) if item.term == "mutex")
        assert boundary.score_without(detail, weights, term) <= router.CAPPED_SCORE

    def test_a_score_can_never_go_below_zero(self):
        _, detail, weights = analyse("look at this", {"look": -1.5, "this": -1.5})
        term = next(item for item in boundary.contributions(detail) if item.term == "this")
        assert boundary.score_without(detail, weights, term) == 0


class TestFlipCandidates:
    def test_names_a_built_in_signal_that_would_route_the_prompt(self):
        prompt, detail, weights = analyse("add a retry with backoff to the api client")
        (description, score, tier), = boundary.flip_candidates(prompt, detail, weights, TWO_TIER)
        assert "task verb" in description and tier == "complex" and score >= 5

    def test_names_learned_terms_that_are_not_in_the_prompt_yet(self):
        prompt, detail, weights = analyse("tidy the module", {"ensure": 1.4, "tidy": 0.4})
        described = " ".join(item[0] for item in boundary.flip_candidates(prompt, detail, weights, TWO_TIER))
        assert '"ensure"' in described and '"tidy"' not in described

    def test_never_suggests_a_term_that_would_not_raise_the_score(self):
        prompt, detail, weights = analyse("tidy the module", {"nope": -1.0, "tidy": 0.4})
        assert all('"nope"' not in item[0] for item in boundary.flip_candidates(prompt, detail, weights, TWO_TIER))

    def test_reports_the_resulting_score_from_the_real_scorer(self):
        prompt, detail, weights = analyse("tidy the module", {"ensure": 1.4})
        candidates = boundary.flip_candidates(prompt, detail, weights, TWO_TIER)
        score = next(score for description, score, _ in candidates if "ensure" in description)
        assert score == router.analyse_prompt("tidy the module ensure", weights)["score"]

    def test_lists_at_most_three(self):
        terms = {f"term{n}": 1.0 + n / 10 for n in range(9)}
        prompt, detail, weights = analyse("tidy the module", terms)
        assert len(boundary.flip_candidates(prompt, detail, weights, TWO_TIER)) <= boundary.MAX_CANDIDATES

    def test_finds_nothing_for_a_prompt_that_already_matches_everything(self):
        prompt = " ".join(k for k, _ in router.STRONG_PATTERNS) + " " + " ".join(
            k for k, _ in router.MODERATE_PATTERNS
        )
        detail = router.analyse_prompt(prompt)
        assert boundary.flip_candidates(prompt, detail, {}, TWO_TIER) == []


class TestRenderRouted:
    def test_reports_the_distance_above_the_threshold(self):
        out = render("refactor the auth module and migrate the schema across the whole codebase")
        assert "4 points clear of the COMPLEX threshold (5)" in out

    def test_lists_what_carried_it_there(self):
        out = render("refactor the auth module and migrate the schema across the whole codebase")
        assert "what carried it there" in out and "task verbs" in out

    def test_says_whether_removing_the_largest_drops_it_back(self):
        out = render("refactor the auth module and migrate the schema across the whole codebase")
        assert "without task verbs" in out and "answered in-session" in out

    def test_says_when_removing_the_largest_would_not_drop_it_back(self):
        out = render(
            "refactor and migrate and rewrite the api schema pipeline\n"
            "traceback (most recent call last)"
        )
        assert "it scores 6 — COMPLEX -> heavy-task-fable" in out

    def test_names_the_middle_tier_it_would_fall_to(self):
        out = render(
            "refactor the auth module and migrate the schema across the whole codebase",
            config=THREE_TIER,
        )
        assert "MODERATE -> mid-task-sonnet" in out

    def test_marks_a_topical_term_among_the_contributions(self):
        out = render(
            "refactor the naplan schema and migrate the api", terms={"naplan": 1.0},
            topical=frozenset({"naplan"}),
        )
        assert "topical: seen in only one project" in out

    def test_notes_that_a_score_sits_exactly_on_the_threshold(self):
        out = render("fix the failing api test", config={**TWO_TIER, "complexity": {"threshold": 3}})
        assert "exactly on the COMPLEX threshold (3)" in out

    def test_truncates_a_long_contribution_list(self):
        out = render(
            "1. refactor the api\n2. migrate the schema\n3. debug the pipeline\n"
            "```\ntraceback (most recent call last)\n```\n" + "word " * 200
        )
        assert "and 1 more" in out


class TestRenderInSession:
    def test_reports_the_distance_below_the_nearest_edge(self):
        out = render("add a retry with backoff to the api client")
        assert "2 points short of the COMPLEX threshold (5)" in out

    def test_reports_a_single_point_in_the_singular(self):
        out = render("add a retry to the api client", config=THREE_TIER)
        assert "1 point short of the MODERATE threshold (3)" in out

    def test_names_what_would_flip_it(self):
        out = render("add a retry with backoff to the api client")
        assert "what would flip it" in out and "task verb" in out and "COMPLEX -> heavy-task-fable" in out

    def test_says_when_nothing_would_flip_it(self):
        out = render("what is a mutex?")
        assert "none of these cross the edge on their own" in out

    def test_reports_a_prompt_that_matches_nothing_at_all(self):
        out = render("hmm")
        assert "no built-in signal matched, so the score started at 0" in out

    def test_explains_a_cap_as_what_is_holding_it_back(self):
        out = render("what is a mutex?", terms={"mutex": 1.4})
        assert "capped to 2 by short question" in out and "no term can lift this" in out

    def test_lists_the_negative_terms_holding_it_back(self):
        out = render("tidy the naplan module", terms={"naplan": -0.9})
        assert "what is holding it back" in out and 'learned term "naplan"' in out and "-0.90" in out

    def test_marks_a_negative_term_as_topical_and_says_it_is_a_sample(self):
        out = render("tidy the naplan module", terms={"naplan": -0.9}, topical=frozenset({"naplan"}))
        assert "topical: seen in only one project" in out
        assert "does the full pass" in out

    def test_says_nothing_about_topicality_without_a_flagged_term(self):
        assert "topical" not in render("tidy the naplan module", terms={"naplan": -0.9})

    def test_omits_the_holding_back_block_when_nothing_is(self):
        assert "holding it back" not in render("add a retry with backoff to the api client")

    def test_survives_a_prompt_with_no_learned_terms_at_all(self):
        out = render("tidy the module", terms={})
        assert "decision boundary" in out


class TestRenderHostileInput:
    @pytest.mark.parametrize("prompt", [
        "'; DROP TABLE prompts; --",
        "$(rm -rf /) `whoami`",
        "../../../../etc/passwd",
        "refactor" * 4000,
        "\x00\x01 refactor the module",
    ])
    def test_never_raises_on_a_hostile_prompt(self, prompt):
        assert "decision boundary" in render(prompt, terms={"refactor": 1.0})

    def test_a_term_that_looks_like_a_regex_is_matched_literally(self):
        out = render("tidy the module", terms={"a-b": 1.5, ".*": 2.0})
        assert ".*" not in out, "an invalid term never reaches the candidate list"

    def test_an_empty_config_still_reports_an_edge(self):
        out = render("refactor the auth module", config={})
        assert "COMPLEX" in out
