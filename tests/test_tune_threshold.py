import json

import pytest

import analyze_history
import tune_threshold as tune

PRICING = {
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
}
CONFIG = {
    "models": {"complex": "fable", "simple": "sonnet"},
    "complexity": {"threshold": 5},
    "pricing_usd_per_mtok": PRICING,
}
USAGE = {
    "input_tokens": 1_000, "output_tokens": 2_000,
    "cache_read_input_tokens": 5_000, "cache_creation_input_tokens": 500,
}

HEAVY_TEXT = "refactor the auth module and migrate the database schema across the whole codebase"
LIGHT_TEXT = "rename it"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """No test may read the operator's real classifier or config."""
    monkeypatch.setenv("MODEL_SWITCHER_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    return tmp_path / "home"


def turn(text=LIGHT_TEXT, *, heavy=False, session="s1", usage=USAGE, score=None):
    made = {
        "text": text, "session": session, "heavy": heavy,
        "tools": 14 if heavy else 1, "mutations": 0, "delegations": 0, "output": 500,
        "usage": {"msg": dict(usage)} if usage is not None else {},
    }
    if score is not None:
        made["score"] = score
    return made


def scored(score, heavy, count=1, cost_simple=1.0, cost_complex=5.0):
    return [
        {"text": "x", "session": "s1", "heavy": heavy, "score": score,
         "cost_simple": cost_simple, "cost_complex": cost_complex}
        for _ in range(count)
    ]


class TestCollect:
    def _write(self, directory, name, records):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )

    def _session(self, text, tools, usage=None):
        return [
            {"type": "user", "sessionId": "s1", "message": {"content": text}},
            {"type": "assistant", "message": {
                "id": "msg_1", "model": "claude-sonnet-5",
                "content": [{"type": "tool_use", "name": t} for t in tools],
                "usage": usage or dict(USAGE),
            }},
        ]

    def test_returns_labelled_prompts_with_their_usage(self, tmp_path):
        self._write(tmp_path / "p", "t", self._session(HEAVY_TEXT, ["Edit"] * 14))
        (found,) = tune.collect([tmp_path / "p"], None)
        assert found["heavy"] is True
        assert list(found["usage"].values()) == [USAGE]

    def test_applies_the_same_filtering_as_learn(self, tmp_path):
        self._write(tmp_path / "p", "t", self._session("yes", ["Edit"] * 14) + self._session("/clear", ["Edit"]))
        assert tune.collect([tmp_path / "p"], None) == []

    def test_survives_a_malformed_transcript(self, tmp_path):
        (tmp_path / "p").mkdir(parents=True)
        (tmp_path / "p" / "t.jsonl").write_text(
            "{ not json\n[1,2,3]\nnull\n" + json.dumps(self._session(HEAVY_TEXT, ["Edit"] * 14)[0]) + "\n"
            + json.dumps(self._session(HEAVY_TEXT, ["Edit"] * 14)[1]) + "\n",
            encoding="utf-8",
        )
        assert len(tune.collect([tmp_path / "p"], None)) == 1

    def test_returns_nothing_for_a_missing_directory(self, tmp_path):
        assert tune.collect([tmp_path / "absent"], None) == []

    def test_honours_the_session_cap(self, tmp_path):
        for i in range(4):
            self._write(tmp_path / "p", f"t{i}", self._session(HEAVY_TEXT, ["Edit"] * 14))
        assert len(tune.collect([tmp_path / "p"], 2)) == 2


class TestCalibration:
    def test_counts_prompts_and_the_work_rate_at_each_score(self):
        rows = tune.calibration(scored(3, True, 3) + scored(3, False, 1) + scored(9, True, 2))
        assert rows[3]["prompts"] == 4 and rows[3]["heavy"] == 3 and rows[3]["rate"] == 0.75
        assert rows[9]["prompts"] == 2 and rows[9]["rate"] == 1.0

    def test_covers_every_score_the_router_can_emit(self):
        rows = tune.calibration(scored(5, True))
        assert [row["score"] for row in rows] == list(range(tune.MAX_SCORE + 1))

    def test_an_empty_band_has_no_rate_rather_than_a_misleading_zero(self):
        rows = tune.calibration(scored(5, True))
        assert rows[0]["prompts"] == 0 and rows[0]["rate"] == 0.0


class TestTierRates:
    def test_resolves_both_tiers_through_the_pricing_aliases(self):
        rates, why_not = tune.tier_rates(CONFIG)
        assert why_not == ""
        assert rates["complex"]["output"] == 50.0 and rates["simple"]["output"] == 10.0

    def test_reports_a_missing_pricing_table(self):
        rates, why_not = tune.tier_rates({"models": {"complex": "fable", "simple": "sonnet"}})
        assert rates == {} and "pricing_usd_per_mtok" in why_not

    @pytest.mark.parametrize("pricing", [
        {"claude-fable-5": {"input": True, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0}},
        {"claude-fable-5": {"input": -1.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0}},
        {"claude-fable-5": "not a rate block"},
        {"claude-fable-5": {"input": 1.0}},
    ])
    def test_an_unusable_rate_block_is_not_a_price(self, pricing):
        rates, why_not = tune.tier_rates({"models": {"complex": "fable", "simple": "sonnet"},
                                          "pricing_usd_per_mtok": pricing})
        assert rates == {} and why_not

    @pytest.mark.parametrize("models", [
        {}, {"complex": "fable"}, {"complex": "fable", "simple": None},
        {"complex": "fable", "simple": "   "}, {"complex": "fable", "simple": 5},
    ])
    def test_reports_an_unset_tier_rather_than_guessing_one(self, models):
        rates, why_not = tune.tier_rates({"models": models, "pricing_usd_per_mtok": PRICING})
        assert rates == {} and "is not set" in why_not

    def test_reports_a_model_with_no_rates_of_its_own(self):
        rates, why_not = tune.tier_rates({"models": {"complex": "mythos", "simple": "sonnet"},
                                          "pricing_usd_per_mtok": PRICING})
        assert rates == {} and "mythos" in why_not

    def test_survives_a_models_section_that_is_not_an_object(self):
        rates, why_not = tune.tier_rates({"models": "fable", "pricing_usd_per_mtok": PRICING})
        assert rates == {} and why_not


class TestPriceTurns:
    def test_prices_each_prompts_own_tokens_at_both_tiers(self):
        turns = [turn()]
        rates, _ = tune.tier_rates(CONFIG)
        assert tune.price_turns(turns, rates) is True
        # 1k in, 2k out, 5k cache read, 500 cache write, at sonnet rates.
        assert turns[0]["cost_simple"] == pytest.approx(
            (1_000 * 2.0 + 2_000 * 10.0 + 5_000 * 0.2 + 500 * 2.5) / 1e6
        )
        assert turns[0]["cost_complex"] == pytest.approx(turns[0]["cost_simple"] * 5)

    def test_a_turn_that_recorded_no_usage_is_not_priceable(self):
        turns = [turn(usage=None)]
        rates, _ = tune.tier_rates(CONFIG)
        assert tune.price_turns(turns, rates) is False
        assert turns[0]["cost_simple"] == 0.0

    def test_a_corpus_of_zero_token_entries_is_not_priceable(self):
        turns = [turn(usage={"input_tokens": 0, "output_tokens": 0})]
        rates, _ = tune.tier_rates(CONFIG)
        assert tune.price_turns(turns, rates) is False

    def test_survives_a_turn_dict_that_never_carried_usage(self):
        """learn's own turn dicts predate the usage key; tune must not require it."""
        bare = {"text": "x", "session": "s", "heavy": False}
        rates, _ = tune.tier_rates(CONFIG)
        assert tune.price_turns([bare], rates) is False
        assert bare["cost_complex"] == 0.0

    def test_fast_mode_is_priced_at_the_fast_rates(self):
        config = json.loads(json.dumps(CONFIG))
        config["pricing_usd_per_mtok"]["claude-fable-5"]["fast"] = {
            "input": 20.0, "output": 100.0, "cache_write": 25.0, "cache_read": 2.0,
        }
        rates, _ = tune.tier_rates(config)
        normal, fast = [turn()], [turn(usage=dict(USAGE, speed="fast"))]
        tune.price_turns(normal, rates)
        tune.price_turns(fast, rates)
        assert fast[0]["cost_complex"] == pytest.approx(normal[0]["cost_complex"] * 2)


class TestCandidates:
    def test_covers_the_useful_range(self):
        assert tune.candidates_for(5) == [3, 4, 5, 6, 7, 8]

    def test_always_includes_the_users_own_threshold(self):
        assert tune.candidates_for(6.5) == [3, 4, 5, 6, 6.5, 7, 8]
        assert tune.candidates_for(1) == [1, 3, 4, 5, 6, 7, 8]

    def test_does_not_duplicate_a_round_threshold(self):
        assert tune.candidates_for(7.0).count(7) == 1


class TestSweep:
    def test_counts_what_each_threshold_would_delegate_and_catch(self):
        turns = scored(9, True, 3) + scored(9, False, 1) + scored(2, True, 1) + scored(2, False, 5)
        (row,) = tune.sweep(turns, [5], priced=False)
        assert row["delegated"] == pytest.approx(4 / 10)
        assert row["precision"] == pytest.approx(3 / 4)
        assert row["recall"] == pytest.approx(3 / 4)
        assert row["f1"] == pytest.approx(0.75)

    def test_a_threshold_nothing_reaches_has_no_precision(self):
        (row,) = tune.sweep(scored(1, True, 4), [9], priced=False)
        assert row["delegated"] == 0.0 and row["precision"] == 0.0 and row["f1"] == 0.0

    def test_a_corpus_with_no_real_work_has_no_recall(self):
        (row,) = tune.sweep(scored(9, False, 4), [5], priced=False)
        assert row["recall"] == 0.0 and row["precision"] == 0.0

    def test_cost_follows_the_tier_each_prompt_would_route_to(self):
        turns = scored(9, True, 1, cost_simple=1.0, cost_complex=5.0)
        turns += scored(2, False, 1, cost_simple=1.0, cost_complex=5.0)
        low, high = tune.sweep(turns, [2, 9], priced=True)
        # At threshold 2 both delegate ($5 each); at 9 only one does ($5 + $1).
        assert low["cost"] == pytest.approx(10.0 / 2 * tune.COST_BASIS_PROMPTS)
        assert high["cost"] == pytest.approx(6.0 / 2 * tune.COST_BASIS_PROMPTS)

    def test_cost_is_absent_rather_than_zero_when_nothing_can_be_priced(self):
        (row,) = tune.sweep(scored(9, True, 2), [5], priced=False)
        assert row["cost"] is None


class TestRecommend:
    def _rows(self, best_threshold, best_f1, current_f1=0.40):
        return [
            {"threshold": t, "f1": best_f1 if t == best_threshold else current_f1,
             "delegated": 0.2, "precision": 0.5, "recall": 0.5, "cost": None}
            for t in (3, 4, 5, 6, 7, 8)
        ]

    def _corpus(self, total=400, heavy=120):
        return scored(9, True, heavy) + scored(1, False, total - heavy)

    def test_refuses_on_a_corpus_too_small_to_conclude_anything(self):
        turns = self._corpus(total=20, heavy=6)
        assert "too few" in tune.recommend(self._rows(3, 0.9), 5, turns)

    def test_refuses_when_every_prompt_carries_the_same_label(self):
        assert "same label" in tune.recommend(self._rows(3, 0.9), 5, self._corpus(400, 400))
        assert "same label" in tune.recommend(self._rows(3, 0.9), 5, self._corpus(400, 0))

    def test_keeps_a_threshold_that_is_already_the_best(self):
        message = tune.recommend(self._rows(5, 0.55), 5, self._corpus())
        assert "keep threshold 5" in message

    def test_says_nothing_beats_it_when_the_margin_is_noise(self):
        rows = self._rows(7, 0.41, current_f1=0.40)
        message = tune.recommend(rows, 5, self._corpus())
        assert "no recommendation" in message and "Leave it alone" in message

    def test_recommends_a_threshold_that_is_clearly_better(self):
        message = tune.recommend(self._rows(7, 0.60, current_f1=0.40), 5, self._corpus())
        assert "try threshold 7" in message and "60.0" in message
        assert "est." not in message, "an unpriced sweep cannot quote a price"

    def test_a_recommendation_that_moves_the_delegated_share_states_its_price(self):
        rows = self._rows(7, 0.60, current_f1=0.40)
        for row in rows:
            row["cost"] = 300.0 if row["threshold"] == 7 else 200.0
        message = tune.recommend(rows, 5, self._corpus())
        assert "est. $300.00 vs $200.00 per 1,000 prompts" in message

    def test_a_tie_resolves_toward_the_threshold_already_in_use(self):
        rows = [{"threshold": t, "f1": 0.5, "delegated": 0.2, "precision": 0.5, "recall": 0.5, "cost": None}
                for t in (3, 4, 5, 6, 7, 8)]
        assert "keep threshold 5" in tune.recommend(rows, 5, self._corpus())

    def test_the_evidence_bar_is_the_one_learn_uses(self):
        assert str(analyze_history.MIN_TRAINABLE_TURNS) in tune.recommend(
            self._rows(3, 0.9), 5, self._corpus(total=10, heavy=3)
        )


def render(config, turns):
    lines = []
    tune.report(config, turns, echo=lines.append)
    return "\n".join(lines)


class TestReport:
    def _corpus(self, count=10):
        return (
            [turn(HEAVY_TEXT, heavy=True, session=f"s{i}") for i in range(count)]
            + [turn(LIGHT_TEXT, heavy=False, session=f"s{i}") for i in range(count)]
        )

    def test_prints_both_tables_with_a_corpus_summary(self):
        out = render(CONFIG, self._corpus())
        assert "20 usable prompts from 10 sessions" in out
        assert "10 became real work, 10 did not" in out
        assert "what prompts at each score actually did:" in out
        assert "what each candidate threshold would do:" in out

    def test_marks_the_current_threshold_in_both_outputs(self):
        out = render(CONFIG, self._corpus())
        assert "<- current threshold (5)" in out
        assert "<- current" in out.split("what each candidate threshold would do:")[1]

    def test_marks_the_first_score_that_would_route_for_a_fractional_threshold(self):
        config = dict(CONFIG, complexity={"threshold": 5.5})
        marked = [line for line in render(config, self._corpus()).splitlines() if "<- current threshold" in line]
        assert len(marked) == 1 and marked[0].strip().startswith("6")

    def test_states_the_cost_estimate_is_not_a_bill(self):
        out = render(CONFIG, self._corpus())
        assert "$/1k prompts" in out and "ESTIMATE, not a quote" in out
        assert "same prompt burns the same" in out

    def test_omits_the_cost_column_and_says_why_when_pricing_is_missing(self):
        out = render({"models": {"complex": "fable", "simple": "sonnet"}}, self._corpus())
        assert "$/1k prompts" not in out
        assert "cost column omitted" in out and "pricing_usd_per_mtok" in out

    def test_omits_the_cost_column_when_no_tokens_were_ever_recorded(self):
        out = render(CONFIG, [turn(HEAVY_TEXT, heavy=True, usage=None) for _ in range(4)])
        assert "$/1k prompts" not in out
        assert "no token usage" in out

    def test_says_when_scoring_used_only_the_built_in_signals(self):
        assert "built-in signals only" in render(CONFIG, self._corpus())

    def test_scores_with_the_installed_classifier_when_there_is_one(self, isolated_home):
        (isolated_home / "classifier.json").write_text(json.dumps(
            {"schema_version": 1, "scoring": {"terms": {"rename": 1.5}, "max_adjustment": 3.0}}
        ), encoding="utf-8")
        assert "1 learned terms" in render(CONFIG, self._corpus())

    def test_notes_that_a_middle_tier_is_not_modelled(self):
        config = dict(CONFIG, models={"complex": "fable", "standard": "sonnet", "simple": "haiku"})
        out = render(config, self._corpus())
        assert "middle tier is configured" in out

    def test_a_two_tier_config_says_nothing_about_a_middle_tier(self):
        assert "middle tier is configured" not in render(CONFIG, self._corpus())

    def test_no_prompt_text_reaches_the_output(self):
        out = render(CONFIG, self._corpus())
        assert "refactor the auth module" not in out and "rename it" not in out

    def test_an_unusable_threshold_falls_back_to_the_default(self):
        out = render(dict(CONFIG, complexity={"threshold": "high"}), self._corpus())
        assert "<- current threshold (5)" in out

    def test_survives_a_corpus_where_every_prompt_scores_the_same(self):
        out = render(CONFIG, [turn(LIGHT_TEXT, heavy=False) for _ in range(5)])
        assert "no recommendation" in out
