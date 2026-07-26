import json

import pytest

import cost_statusline as statusline

PRICING = {
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3},
    "claude-opus-4-8": {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.5},
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_SWITCHER_HOME", str(tmp_path))
    return tmp_path


def write_config(home, pricing=PRICING, wrap_command=None):
    (home / "config.json").write_text(
        json.dumps({"pricing_usd_per_mtok": pricing, "statusline": {"wrap_command": wrap_command}})
    )


def transcript_line(entry_type, msg_id=None, model="claude-sonnet-5", usage=None, is_meta=False, sidechain=False):
    if entry_type == "user":
        return json.dumps(
            {"type": "user", "isMeta": is_meta, "isSidechain": sidechain, "message": {"content": "next prompt"}}
        )
    return json.dumps(
        {"type": "assistant", "isSidechain": sidechain, "message": {"id": msg_id, "model": model, "usage": usage}}
    )


def tool_result_line(timestamp=None):
    """Claude Code writes every tool result back as a type:"user" record with no text block."""
    body = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
    if timestamp:
        body["timestamp"] = timestamp
    return json.dumps(body)


def write_transcript(tmp_path, lines):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def stdin_payload(transcript_path=None, model_name="Sonnet 5", builtin_cost=None):
    payload = {"model": {"id": "claude-sonnet-5", "display_name": model_name}}
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    if builtin_cost is not None:
        payload["cost"] = {"total_cost_usd": builtin_cost}
    return json.dumps(payload)


USAGE = {
    "input_tokens": 1000,
    "cache_creation_input_tokens": 2000,
    "cache_read_input_tokens": 10000,
    "output_tokens": 500,
}
# 1000*3 + 2000*3.75 + 10000*0.3 + 500*15 = 3000+7500+3000+7500 = 21000 / 1e6 = $0.021
USAGE_COST = 0.021


class TestCostMath:
    def test_usage_cost_covers_all_four_rates(self):
        assert statusline.usage_cost(USAGE, PRICING["claude-sonnet-5"]) == pytest.approx(USAGE_COST)

    def test_non_int_token_counts_ignored(self):
        usage = {"input_tokens": "many", "output_tokens": 500}
        assert statusline.usage_cost(usage, PRICING["claude-sonnet-5"]) == pytest.approx(0.0075)

    def test_prefix_match_for_dated_model_id(self):
        assert statusline.match_pricing("claude-sonnet-5-20250929", PRICING) == PRICING["claude-sonnet-5"]

    def test_a_shared_prefix_without_a_separator_is_not_a_match(self):
        # claude-sonnet-55 is a different model than claude-sonnet-5, not a longer spelling of it.
        assert statusline.match_pricing("claude-sonnet-55", PRICING) is None

    def test_negative_token_counts_are_poison_not_credit(self):
        usage = {"input_tokens": -1_000_000, "output_tokens": 500}
        assert statusline.usage_cost(usage, PRICING["claude-sonnet-5"]) == pytest.approx(0.0075)

    def test_boolean_token_counts_are_ignored(self):
        assert statusline.usage_cost({"input_tokens": True}, PRICING["claude-sonnet-5"]) == 0.0

    def test_longest_prefix_wins(self):
        pricing = dict(PRICING, **{"claude-sonnet-5-20250929": {k: 1.0 for k in statusline.RATE_KEYS}})
        assert statusline.match_pricing("claude-sonnet-5-20250929", pricing)["input"] == 1.0

    def test_unknown_model_returns_none(self):
        assert statusline.match_pricing("gpt-4o", PRICING) is None

    def test_entry_with_null_rate_is_unusable(self):
        rates = {"input": 3.0, "output": None, "cache_write": 1, "cache_read": 1}
        config = {"pricing_usd_per_mtok": {"claude-sonnet-5": rates}}
        assert statusline.usable_pricing(config) == {}


class TestRun:
    def test_session_and_turn_costs_computed(self, home, tmp_path):
        write_config(home)
        transcript = write_transcript(
            tmp_path,
            [
                transcript_line("user"),
                transcript_line("assistant", msg_id="m1", usage=USAGE),
                transcript_line("user"),
                transcript_line("assistant", msg_id="m2", usage=USAGE),
            ],
        )
        line = statusline.run(stdin_payload(transcript))
        assert line.startswith("Sonnet 5 | ")
        assert f"turn {statusline.format_cost(USAGE_COST)}" in line
        assert f"session {statusline.format_cost(2 * USAGE_COST)}" in line
        assert "26.0k in / 1.0k out" in line

    def test_duplicate_message_ids_deduped(self, home, tmp_path):
        write_config(home)
        lines = [transcript_line("user")] + [transcript_line("assistant", msg_id="m1", usage=USAGE)] * 3
        line = statusline.run(stdin_payload(write_transcript(tmp_path, lines)))
        assert f"session {statusline.format_cost(USAGE_COST)}" in line

    def test_sidechain_usage_counted_in_session(self, home, tmp_path):
        write_config(home)
        transcript = write_transcript(
            tmp_path,
            [
                transcript_line("user"),
                transcript_line("assistant", msg_id="m1", model="claude-opus-4-8", usage=USAGE, sidechain=True),
            ],
        )
        line = statusline.run(stdin_payload(transcript))
        assert "session $0.19" not in line  # opus rates: 1000*15+2000*18.75+10000*1.5+500*75 = 105000/1e6
        assert f"session {statusline.format_cost(0.1050)}" in line

    def test_ttl_only_cache_writes_appear_in_the_token_count(self, home, tmp_path):
        # These tokens are charged, so a display that reads only the flat total would
        # show dollars beside "0 in".
        write_config(home)
        usage = {"cache_creation": {"ephemeral_1h_input_tokens": 1_000_000, "ephemeral_5m_input_tokens": 0}}
        transcript = write_transcript(
            tmp_path, [transcript_line("user"), transcript_line("assistant", msg_id="m1", usage=usage)],
        )
        line = statusline.run(stdin_payload(transcript))
        assert "1.0M in" in line and "session $3.75" in line

    def test_sidechain_user_message_does_not_reset_turn(self, home, tmp_path):
        write_config(home)
        transcript = write_transcript(
            tmp_path,
            [
                transcript_line("user"),
                transcript_line("assistant", msg_id="m1", usage=USAGE),
                transcript_line("user", sidechain=True),
                transcript_line("assistant", msg_id="m2", usage=USAGE),
            ],
        )
        line = statusline.run(stdin_payload(transcript))
        assert f"turn {statusline.format_cost(2 * USAGE_COST)}" in line

    def test_unconfigured_pricing_shows_link(self, home, tmp_path):
        write_config(home, pricing={"claude-sonnet-5": {k: None for k in statusline.RATE_KEYS}})
        line = statusline.run(stdin_payload())
        assert "cost n/a" in line
        assert statusline.PRICING_URL in line
        assert "config.json" in line

    def test_missing_config_shows_link(self, home):
        assert statusline.PRICING_URL in statusline.run(stdin_payload())

    def test_transcript_without_usage_falls_back_to_builtin(self, home, tmp_path):
        write_config(home)
        transcript = write_transcript(tmp_path, [transcript_line("user")])
        line = statusline.run(stdin_payload(transcript, builtin_cost=1.234))
        assert "session ~$1.23 (builtin est.)" in line

    def test_no_transcript_and_no_builtin(self, home):
        write_config(home)
        assert "cost n/a: no usage data" in statusline.run(stdin_payload())

    def test_unknown_model_flagged_not_crashed(self, home, tmp_path):
        write_config(home)
        transcript = write_transcript(
            tmp_path,
            [
                transcript_line("assistant", msg_id="m1", usage=USAGE),
                transcript_line("assistant", msg_id="m2", model="claude-nova-9", usage=USAGE),
            ],
        )
        line = statusline.run(stdin_payload(transcript))
        assert "no rate: claude-nova-9" in line
        assert f"session {statusline.format_cost(USAGE_COST)}" in line

    def test_malformed_transcript_lines_skipped(self, home, tmp_path):
        write_config(home)
        transcript = write_transcript(
            tmp_path,
            ["not json at all", '"just a string"', transcript_line("assistant", msg_id="m1", usage=USAGE)],
        )
        assert f"session {statusline.format_cost(USAGE_COST)}" in statusline.run(stdin_payload(transcript))

    def test_malformed_stdin_prints_fallback(self, home):
        assert statusline.run("{{{") == "model-switcher: no statusline data"

    def test_wrap_command_output_prefixes_line(self, home):
        write_config(home, wrap_command="echo 'MyStatus | ctx 40%'")
        line = statusline.run(stdin_payload())
        assert line.startswith("MyStatus | ctx 40% | ")

    def test_failing_wrap_command_falls_back_to_model_name(self, home):
        write_config(home, wrap_command="exit 1")
        assert statusline.run(stdin_payload()).startswith("Sonnet 5 | ")

    def test_main_always_prints_a_line(self, home, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
        assert statusline.main() == 0
        assert capsys.readouterr().out.strip() != ""


class TestPromptDetection:
    def test_typed_text_is_a_prompt(self):
        assert statusline._is_prompt({"content": "fix the bug"})
        assert statusline._is_prompt({"content": [{"type": "text", "text": "fix the bug"}]})

    def test_tool_results_are_not_prompts(self):
        content = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
        assert not statusline._is_prompt({"content": content})

    @pytest.mark.parametrize("message", [{}, "nope", None, {"content": "   "}, {"content": 5}])
    def test_blank_or_malformed_messages_are_not_prompts(self, message):
        assert not statusline._is_prompt(message)


class TestTurnBoundary:
    """`turn $` is the cost since the last typed prompt — tool round-trips must not reset it."""

    def test_tool_results_do_not_reset_the_turn(self, home, tmp_path):
        write_config(home)
        transcript = write_transcript(
            tmp_path,
            [
                transcript_line("user"),
                transcript_line("assistant", msg_id="m1", usage=USAGE),
                tool_result_line(),
                transcript_line("assistant", msg_id="m2", usage=USAGE),
            ],
        )
        line = statusline.run(stdin_payload(transcript))
        assert f"turn {statusline.format_cost(2 * USAGE_COST)}" in line

    def test_a_new_prompt_still_resets_the_turn(self, home, tmp_path):
        write_config(home)
        transcript = write_transcript(
            tmp_path,
            [
                transcript_line("user"),
                transcript_line("assistant", msg_id="m1", usage=USAGE),
                tool_result_line(),
                transcript_line("user"),
                transcript_line("assistant", msg_id="m2", usage=USAGE),
            ],
        )
        line = statusline.run(stdin_payload(transcript))
        assert f"turn {statusline.format_cost(USAGE_COST)}" in line


class TestFormatting:
    def test_small_cost_four_decimals(self):
        assert statusline.format_cost(0.0042) == "$0.0042"

    def test_large_cost_two_decimals(self):
        assert statusline.format_cost(12.3456) == "$12.35"

    def test_token_humanisation(self):
        assert statusline.format_tokens(950) == "950"
        assert statusline.format_tokens(12_400) == "12.4k"
        assert statusline.format_tokens(2_500_000) == "2.5M"


RATES_WITH_TTL = {
    "input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5,
}
RATES_LEGACY = {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.5}


class TestCacheWriteTtlSplit:
    """Claude Code caches with a 1-hour TTL, which costs 2x input rather than 1.25x."""

    def test_splits_a_breakdown_into_ttl_buckets(self):
        usage = {
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
        }
        assert statusline.cache_write_tokens(usage) == (100, 200, 0)

    def test_falls_back_to_the_flat_total_when_no_breakdown_exists(self):
        assert statusline.cache_write_tokens({"cache_creation_input_tokens": 300}) == (0, 0, 300)

    def test_falls_back_when_the_breakdown_is_present_but_empty(self):
        usage = {
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        }
        assert statusline.cache_write_tokens(usage) == (0, 0, 300)

    def test_the_breakdown_wins_when_it_disagrees_with_the_flat_total(self):
        # Some real entries carry a total that is smaller than the breakdown beside it.
        # Mixing the two would produce a negative bucket.
        usage = {
            "cache_creation_input_tokens": 91451,
            "cache_creation": {"ephemeral_1h_input_tokens": 119626, "ephemeral_5m_input_tokens": 0},
        }
        five, hour, unsplit = statusline.cache_write_tokens(usage)
        assert (five, hour, unsplit) == (0, 119626, 0)
        assert min(five, hour, unsplit) >= 0

    @pytest.mark.parametrize("breakdown", ["nope", 5, [1, 2], None, {"ephemeral_1h_input_tokens": "lots"}])
    def test_survives_a_malformed_breakdown(self, breakdown):
        usage = {"cache_creation_input_tokens": 300, "cache_creation": breakdown}
        assert statusline.cache_write_tokens(usage) == (0, 0, 300)

    def test_one_hour_writes_cost_more_than_five_minute_writes(self):
        five = {"cache_creation": {"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 0}}
        hour = {"cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1_000_000}}
        assert statusline.usage_cost(five, RATES_WITH_TTL) == pytest.approx(6.25)
        assert statusline.usage_cost(hour, RATES_WITH_TTL) == pytest.approx(10.0)

    def test_a_config_without_a_one_hour_rate_prices_exactly_as_before(self):
        usage = {"cache_creation": {"ephemeral_1h_input_tokens": 1_000_000, "ephemeral_5m_input_tokens": 0}}
        legacy_equivalent = {"cache_creation_input_tokens": 1_000_000}
        assert statusline.usage_cost(usage, RATES_LEGACY) == statusline.usage_cost(
            legacy_equivalent, RATES_LEGACY
        )


class TestFastMode:
    def test_fast_turns_use_the_fast_rate_block(self):
        rates = {**RATES_WITH_TTL, "fast": {**RATES_WITH_TTL, "output": 50.0}}
        usage = {"output_tokens": 1_000_000, "speed": "fast"}
        assert statusline.usage_cost(usage, statusline.effective_rates(usage, rates)) == pytest.approx(50.0)

    def test_standard_turns_use_the_base_rates(self):
        rates = {**RATES_WITH_TTL, "fast": {**RATES_WITH_TTL, "output": 50.0}}
        usage = {"output_tokens": 1_000_000, "speed": "standard"}
        assert statusline.usage_cost(usage, statusline.effective_rates(usage, rates)) == pytest.approx(25.0)

    def test_a_fast_turn_without_a_fast_rate_block_falls_back_to_base(self):
        usage = {"output_tokens": 1_000_000, "speed": "fast"}
        assert statusline.effective_rates(usage, RATES_WITH_TTL) is RATES_WITH_TTL

    def test_a_malformed_fast_block_is_ignored(self):
        rates = {**RATES_WITH_TTL, "fast": "premium"}
        usage = {"speed": "fast"}
        assert statusline.effective_rates(usage, rates) is rates


class TestRateValidation:
    @pytest.mark.parametrize("bad", [True, False, -1, float("inf"), float("nan"), "5.0", None, 1e9])
    def test_rejects_a_model_whose_rate_is_not_a_usable_number(self, bad):
        config = {"pricing_usd_per_mtok": {"m": {**RATES_LEGACY, "input": bad}}}
        assert statusline.usable_pricing(config) == {}

    def test_carries_the_optional_one_hour_rate_through(self):
        config = {"pricing_usd_per_mtok": {"m": dict(RATES_WITH_TTL)}}
        assert statusline.usable_pricing(config)["m"]["cache_write_1h"] == 10.0

    def test_drops_an_unusable_one_hour_rate_but_keeps_the_model(self):
        config = {"pricing_usd_per_mtok": {"m": {**RATES_LEGACY, "cache_write_1h": "free"}}}
        rates = statusline.usable_pricing(config)["m"]
        assert "cache_write_1h" not in rates and rates["input"] == 5.0

    def test_drops_an_unusable_fast_block_but_keeps_the_model(self):
        config = {"pricing_usd_per_mtok": {"m": {**RATES_LEGACY, "fast": {"input": 1}}}}
        rates = statusline.usable_pricing(config)["m"]
        assert "fast" not in rates and rates["output"] == 25.0

    def test_a_zero_rate_is_allowed(self):
        config = {"pricing_usd_per_mtok": {"m": {**RATES_LEGACY, "cache_read": 0}}}
        assert statusline.usable_pricing(config)["m"]["cache_read"] == 0.0


TIERED = {
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3},
}


def totals(session=1.0, baseline=3.0, turn=0.5, tokens_in=100, tokens_out=10, unknown=None):
    return {
        "turn": turn, "session": session, "baseline": baseline, "tokens_in": tokens_in,
        "tokens_out": tokens_out, "unknown": unknown or set(), "baseline_model": "claude-fable-5",
        "models": {"claude-sonnet-5", "claude-fable-5"},
    }


class TestBaselineModel:
    def test_picks_the_dearest_model_the_session_actually_ran(self):
        observed = {"claude-sonnet-5", "claude-fable-5"}
        assert statusline.baseline_model({}, TIERED, observed) == "claude-fable-5"

    def test_ignores_a_model_the_session_never_ran(self):
        # models.complex names fable, but nothing was delegated to it, so it cannot be the
        # yardstick: the session's own dearest model is.
        config = {"models": {"complex": "fable"}}
        observed = {"claude-sonnet-5", "claude-sonnet-4-6"}
        assert statusline.baseline_model(config, TIERED, observed) == "claude-sonnet-4-6"

    def test_an_explicit_override_wins_when_the_session_ran_it(self):
        config = {"statusline": {"savings_baseline": "claude-sonnet-4-6"}}
        observed = {"claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5"}
        assert statusline.baseline_model(config, TIERED, observed) == "claude-sonnet-4-6"

    def test_an_override_the_session_never_ran_is_ignored(self):
        # The override picks among observed models; it cannot introduce a counterfactual one.
        config = {"statusline": {"savings_baseline": "claude-fable-5"}}
        observed = {"claude-sonnet-5", "claude-sonnet-4-6"}
        assert statusline.baseline_model(config, TIERED, observed) == "claude-sonnet-4-6"

    def test_an_override_naming_an_unpriced_model_is_ignored(self):
        config = {"statusline": {"savings_baseline": "nope"}}
        assert statusline.baseline_model(config, TIERED, {"claude-sonnet-5"}) == "claude-sonnet-5"

    @pytest.mark.parametrize("observed", [set(), {"gpt-4"}, {""}, {"claude-fable"}])
    def test_returns_nothing_without_a_priced_candidate(self, observed):
        assert statusline.baseline_model({}, TIERED, observed) is None

    def test_breaks_ties_deterministically(self):
        table = {"model-b": dict(TIERED["claude-sonnet-5"]), "model-a": dict(TIERED["claude-sonnet-5"])}
        observed = {"model-a", "model-b"}
        reversed_table = dict(reversed(list(table.items())))
        assert statusline.baseline_model({}, table, observed) == "model-a"
        assert statusline.baseline_model({}, reversed_table, observed) == "model-a"


class TestSavingsSegment:
    def test_reports_what_routing_avoided_and_names_the_yardstick(self):
        rendered = statusline._segment_saved(totals(session=1.0, baseline=3.0), {})
        assert "saved" in rendered and "67%" in rendered and "vs fable-5" in rendered

    def test_omits_the_yardstick_when_it_is_unknown(self):
        rendered = statusline._segment_saved({**totals(), "baseline_model": None}, {})
        assert "vs" not in rendered

    def test_is_silent_when_nothing_was_saved(self):
        assert statusline._segment_saved(totals(session=3.0, baseline=3.0), {}) is None

    def test_is_silent_when_the_session_cost_more_than_the_baseline(self):
        assert statusline._segment_saved(totals(session=5.0, baseline=3.0), {}) is None

    def test_is_silent_without_a_baseline(self):
        assert statusline._segment_saved(totals(baseline=0.0), {}) is None

    def test_is_silent_below_half_a_cent(self):
        assert statusline._segment_saved(totals(session=1.0, baseline=1.004), {}) is None


class TestSavingsCannotBeFabricated:
    """A single-model session saved nothing, whatever the configured tiers cost.

    The regression this guards: pricing every turn at models.complex produced
    'saved == session cost' for any session whose model was exactly half that price —
    a number generated by the rate ratio rather than by any routing decision.
    """

    def _single_model_totals(self, complex_alias):
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        entries = [{"model": "claude-sonnet-5", "usage": usage, "line": 1}]
        config = {"models": {"complex": complex_alias, "simple": "sonnet"}}
        return statusline.summarise(entries, TIERED, config, last_user_line=0)

    @pytest.mark.parametrize("complex_alias", ["fable", "claude-fable-5", "claude-sonnet-4-6"])
    def test_no_savings_are_claimed_for_a_single_model_session(self, complex_alias):
        result = self._single_model_totals(complex_alias)
        assert result["baseline"] == 0.0
        assert statusline._segment_saved(result, {}) is None

    def test_saved_never_equals_the_session_cost(self):
        # claude-fable-5 is exactly 2x claude-sonnet-5 on every rate — the shape that
        # produced the bogus "session $90.05 | saved $90.05 (50%)".
        result = self._single_model_totals("fable")
        assert result["session"] > 0
        assert result["baseline"] - result["session"] != pytest.approx(result["session"])

    def test_an_explicit_override_cannot_resurrect_it(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        entries = [{"model": "claude-sonnet-5", "usage": usage, "line": 1}]
        config = {"statusline": {"savings_baseline": "claude-fable-5"}}
        result = statusline.summarise(entries, TIERED, config, last_user_line=0)
        assert result["baseline"] == 0.0

    def test_savings_appear_once_the_session_genuinely_spans_tiers(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        entries = [
            {"model": "claude-sonnet-5", "usage": usage, "line": 1},
            {"model": "claude-fable-5", "usage": usage, "line": 2},
        ]
        result = statusline.summarise(entries, TIERED, {}, last_user_line=0)
        # $12 on sonnet + $60 on fable = $72 actual; all-fable would have been $120.
        assert result["session"] == pytest.approx(72.0)
        assert result["baseline"] == pytest.approx(120.0)
        assert "saved $48.00 (40% vs fable-5)" == statusline._segment_saved(result, {})

    def test_an_unpriced_model_does_not_count_as_a_second_tier(self):
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        entries = [
            {"model": "claude-sonnet-5", "usage": usage, "line": 1},
            {"model": "claude-unknown-9", "usage": usage, "line": 2},
        ]
        result = statusline.summarise(entries, TIERED, {}, last_user_line=0)
        assert result["baseline"] == 0.0 and result["unknown"] == {"claude-unknown-9"}


class TestRoutingSegment:
    def test_says_when_routing_is_off(self):
        assert statusline._segment_routing({}, {"routing": {"enabled": False}}) == "routing off"

    def test_says_when_a_third_tier_is_active(self):
        config = {"models": {"complex": "fable", "standard": "sonnet", "simple": "haiku"}}
        assert statusline._segment_routing({}, config) == "3 tiers"

    def test_stays_quiet_for_the_two_tier_default(self):
        assert statusline._segment_routing({}, {"models": {"complex": "fable"}}) is None

    def test_respects_a_two_tier_override(self):
        config = {"models": {"complex": "f", "standard": "s"}, "routing": {"tiers": 2}}
        assert statusline._segment_routing({}, config) is None

    @pytest.mark.parametrize("routing", ["nope", {"enabled": "false"}, {"tiers": True}, {}])
    def test_survives_a_malformed_routing_block(self, routing):
        assert statusline._segment_routing({}, {"routing": routing}) is None

    def test_renders_the_model_ladder(self):
        config = {"models": {"complex": "fable", "standard": "sonnet", "simple": "haiku"}}
        assert statusline._segment_models({}, config) == "haiku > sonnet > fable"

    def test_model_ladder_is_silent_without_models(self):
        assert statusline._segment_models({}, {"models": "nope"}) is None


class TestSegmentSelection:
    def test_defaults_when_unconfigured(self):
        assert statusline.configured_segments({}) == statusline.DEFAULT_SEGMENTS

    def test_honours_an_explicit_order(self):
        config = {"statusline": {"segments": ["session", "turn"]}}
        assert statusline.configured_segments(config) == ("session", "turn")

    def test_drops_unknown_names(self):
        config = {"statusline": {"segments": ["turn", "teleport"]}}
        assert statusline.configured_segments(config) == ("turn",)

    @pytest.mark.parametrize("segments", [[], "turn", None, ["nonsense"], [5, {}]])
    def test_falls_back_to_defaults_for_anything_unusable(self, segments):
        assert statusline.configured_segments({"statusline": {"segments": segments}}) == (
            statusline.DEFAULT_SEGMENTS
        )


class TestDashboardLine:
    def _session(self, tmp_path, home, config_extra=None, models=("claude-sonnet-5", "claude-fable-5")):
        write_config(home, TIERED)
        config = json.loads((home / "config.json").read_text())
        config.update(config_extra or {})
        (home / "config.json").write_text(json.dumps(config))
        usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        lines = [transcript_line("user")]
        lines += [
            transcript_line("assistant", msg_id=f"a{i}", model=model, usage=usage)
            for i, model in enumerate(models)
        ]
        return stdin_payload(write_transcript(tmp_path, lines))

    def test_shows_savings_against_the_dearest_model_the_session_ran(self, tmp_path, home):
        payload = self._session(tmp_path, home, {"models": {"complex": "fable", "simple": "sonnet"}})
        line = statusline.run(payload)
        # 1M in + 1M out costs $12 on sonnet-5 and $60 on fable-5; all-fable would be $120.
        assert "saved $48.00 (40% vs fable-5)" in line

    def test_omits_savings_when_the_session_never_left_one_model(self, tmp_path, home):
        payload = self._session(
            tmp_path, home, {"models": {"complex": "fable", "simple": "sonnet"}},
            models=("claude-sonnet-5",),
        )
        assert "saved" not in statusline.run(payload)

    def test_omits_savings_when_the_session_ran_only_on_the_heavy_model(self, tmp_path, home):
        payload = self._session(
            tmp_path, home, {"models": {"complex": "fable", "simple": "sonnet"}},
            models=("claude-fable-5", "claude-fable-5"),
        )
        assert "saved" not in statusline.run(payload)

    def test_keeps_reporting_turn_session_and_tokens(self, tmp_path, home):
        payload = self._session(tmp_path, home, {"models": {"complex": "fable", "simple": "sonnet"}})
        line = statusline.run(payload)
        assert "turn " in line and "session " in line and "in / " in line and "out" in line

    def test_surfaces_routing_being_off(self, tmp_path, home):
        payload = self._session(tmp_path, home, {
            "models": {"complex": "fable", "simple": "sonnet"}, "routing": {"enabled": False},
        })
        assert "routing off" in statusline.run(payload)

    def test_respects_a_custom_segment_list(self, tmp_path, home):
        payload = self._session(tmp_path, home, {
            "models": {"complex": "fable", "simple": "sonnet"},
            "statusline": {"segments": ["saved"], "wrap_command": None},
        })
        line = statusline.run(payload)
        assert "saved" in line and "turn " not in line and "session " not in line

    def test_still_flags_a_model_with_no_rate(self, tmp_path, home):
        write_config(home, TIERED)
        lines = [
            transcript_line("user"),
            transcript_line("assistant", msg_id="a", model="claude-unknown-9", usage={"output_tokens": 5}),
        ]
        assert "no rate: claude-unknown-9" in statusline.run(stdin_payload(write_transcript(tmp_path, lines)))


class TestUnpricedModels:
    """A missing rate only matters when there was something to charge for."""

    SYNTHETIC = {
        "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
    }

    def _summarise(self, entries):
        return statusline.summarise(entries, TIERED, {}, last_user_line=0)

    def test_a_zero_token_placeholder_is_not_reported_as_unpriced(self):
        # Claude Code logs <synthetic> for interrupts and error messages: no model, no tokens.
        result = self._summarise([{"model": "<synthetic>", "usage": self.SYNTHETIC, "line": 1}])
        assert result["unknown"] == set()

    def test_an_unpriced_model_that_actually_billed_is_still_reported(self):
        entries = [{"model": "claude-unknown-9", "usage": {"output_tokens": 5}, "line": 1}]
        assert self._summarise(entries)["unknown"] == {"claude-unknown-9"}

    @pytest.mark.parametrize("field", [
        "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
    ])
    def test_any_single_billable_field_is_enough_to_report_it(self, field):
        entries = [{"model": "mystery", "usage": {**self.SYNTHETIC, field: 1}, "line": 1}]
        assert self._summarise(entries)["unknown"] == {"mystery"}

    def test_a_ttl_split_cache_write_counts_as_billable(self):
        usage = {**self.SYNTHETIC, "cache_creation": {"ephemeral_1h_input_tokens": 7}}
        assert self._summarise([{"model": "mystery", "usage": usage, "line": 1}])["unknown"] == {"mystery"}

    def test_a_model_is_reported_once_it_bills_anywhere(self):
        entries = [
            {"model": "mystery", "usage": self.SYNTHETIC, "line": 1},
            {"model": "mystery", "usage": {"output_tokens": 100}, "line": 2},
        ]
        assert self._summarise(entries)["unknown"] == {"mystery"}

    def test_placeholders_do_not_suppress_a_real_warning(self):
        entries = [
            {"model": "<synthetic>", "usage": self.SYNTHETIC, "line": 1},
            {"model": "claude-unknown-9", "usage": {"output_tokens": 5}, "line": 2},
        ]
        assert self._summarise(entries)["unknown"] == {"claude-unknown-9"}

    def test_the_line_stays_clean_when_only_placeholders_are_unpriced(self, tmp_path, home):
        write_config(home, TIERED)
        lines = [
            transcript_line("user"),
            transcript_line("assistant", msg_id="a", model="claude-sonnet-5",
                            usage={"input_tokens": 1000, "output_tokens": 100}),
            transcript_line("assistant", msg_id="b", model="<synthetic>", usage=self.SYNTHETIC),
        ]
        line = statusline.run(stdin_payload(write_transcript(tmp_path, lines)))
        assert "no rate" not in line and "session " in line


class TestBillableTokens:
    def test_counts_every_charging_field(self):
        usage = {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 4,
                 "cache_creation_input_tokens": 8}
        assert statusline.billable_tokens(usage) == 15

    def test_prefers_the_ttl_breakdown_over_the_total(self):
        usage = {"cache_creation_input_tokens": 999,
                 "cache_creation": {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 4}}
        assert statusline.billable_tokens(usage) == 7

    @pytest.mark.parametrize("usage", [{}, {"input_tokens": None}, {"output_tokens": "many"}, {"input_tokens": -5}])
    def test_treats_unusable_values_as_nothing(self, usage):
        assert statusline.billable_tokens(usage) == 0

    def test_a_negative_field_cannot_cancel_a_real_one(self):
        # A poisoned negative must not suppress the `no rate:` warning for tokens that billed.
        assert statusline.billable_tokens({"input_tokens": -5, "output_tokens": 5}) == 5


class TestSubagentTranscripts:
    """Claude Code writes each spawned agent to <project>/<session-id>/**/*.jsonl, outside the
    session file. Reading only the session file misses everything every agent did."""

    USAGE = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}

    def _session(self, tmp_path, main_lines, agents=None):
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("\n".join(main_lines) + "\n")
        for name, lines in (agents or {}).items():
            path = tmp_path / "session" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + "\n")
        return transcript

    def _line(self, kind, msg_id=None, model="claude-sonnet-5", usage=None, timestamp=None):
        if kind == "user":
            body = {"type": "user", "message": {"content": [{"type": "text", "text": "go"}]}}
        else:
            body = {"type": "assistant", "message": {"id": msg_id, "model": model, "usage": usage}}
        if timestamp:
            body["timestamp"] = timestamp
        return json.dumps(body)

    def test_no_agent_directory_means_no_extra_entries(self, tmp_path):
        transcript = self._session(tmp_path, [self._line("user"), self._line("a", "m1", usage=self.USAGE)])
        entries, _ = statusline.collect_entries(transcript)
        assert len(entries) == 1

    def test_finds_agents_under_the_session_directory(self, tmp_path):
        transcript = self._session(
            tmp_path,
            [self._line("user"), self._line("a", "m1", usage=self.USAGE)],
            {"subagents/agent-x.jsonl": [self._line("a", "m2", "claude-fable-5", self.USAGE)]},
        )
        entries, _ = statusline.collect_entries(transcript)
        assert sorted(entry["model"] for entry in entries) == ["claude-fable-5", "claude-sonnet-5"]

    def test_finds_workflow_agents_nested_deeper(self, tmp_path):
        transcript = self._session(
            tmp_path, [self._line("user")],
            {"wf_abc123/agent-y.jsonl": [self._line("a", "m3", "claude-fable-5", self.USAGE)]},
        )
        assert len(statusline.collect_entries(transcript)[0]) == 1

    def test_agent_cost_reaches_the_session_total(self, tmp_path, home):
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user"), self._line("a", "m1", "claude-sonnet-5", self.USAGE)],
            {"subagents/agent-x.jsonl": [self._line("a", "m2", "claude-fable-5", self.USAGE)]},
        )
        line = statusline.run(stdin_payload(transcript))
        # $12 on sonnet-5 in the session + $60 on fable-5 in the agent.
        assert "session $72.00" in line

    def test_a_delegating_session_can_finally_show_savings(self, tmp_path, home):
        # Savings need two priced models; before agent transcripts were read, the delegated
        # model lived in a file the statusline never opened, so this could not happen.
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user"), self._line("a", "m1", "claude-sonnet-5", self.USAGE)],
            {"subagents/agent-x.jsonl": [self._line("a", "m2", "claude-fable-5", self.USAGE)]},
        )
        assert "saved $48.00 (40% vs fable-5)" in statusline.run(stdin_payload(transcript))

    def test_agent_work_after_the_last_prompt_counts_towards_the_turn(self, tmp_path, home):
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user", timestamp="2026-07-25T10:00:00Z")],
            {"subagents/agent-x.jsonl": [
                self._line("a", "m2", "claude-sonnet-5", self.USAGE, timestamp="2026-07-25T10:05:00Z")]},
        )
        assert "turn $12.00" in statusline.run(stdin_payload(transcript))

    def test_agent_work_from_an_earlier_turn_is_session_only(self, tmp_path, home):
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user", timestamp="2026-07-25T10:00:00Z")],
            {"subagents/agent-x.jsonl": [
                self._line("a", "m2", "claude-sonnet-5", self.USAGE, timestamp="2026-07-25T09:00:00Z")]},
        )
        line = statusline.run(stdin_payload(transcript))
        assert "turn $0.0000" in line and "session $12.00" in line

    def test_without_timestamps_agent_work_never_inflates_the_turn(self, tmp_path, home):
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path, [self._line("user")],
            {"subagents/agent-x.jsonl": [self._line("a", "m2", "claude-sonnet-5", self.USAGE)]},
        )
        line = statusline.run(stdin_payload(transcript))
        assert "turn $0.0000" in line and "session $12.00" in line

    def test_agent_work_between_prompt_and_tool_result_stays_in_the_turn(self, tmp_path, home):
        # The tool result lands after the agent finishes; it must not become the boundary
        # that pushes the agent's cost out of the turn.
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user", timestamp="2026-07-25T10:00:00Z"),
             tool_result_line(timestamp="2026-07-25T10:10:00Z")],
            {"subagents/agent-x.jsonl": [
                self._line("a", "m2", "claude-sonnet-5", self.USAGE, timestamp="2026-07-25T10:05:00Z")]},
        )
        assert "turn $12.00" in statusline.run(stdin_payload(transcript))

    def test_an_entry_recorded_in_both_files_is_paid_once(self, tmp_path, home):
        write_config(home, TIERED)
        shared = self._line("a", "m1", "claude-sonnet-5", self.USAGE)
        transcript = self._session(
            tmp_path, [self._line("user"), shared], {"subagents/agent-x.jsonl": [shared]},
        )
        assert "session $12.00" in statusline.run(stdin_payload(transcript))

    def test_the_same_id_in_two_agent_files_is_paid_once(self, tmp_path, home):
        write_config(home, TIERED)
        shared = self._line("a", "m1", "claude-sonnet-5", self.USAGE)
        transcript = self._session(
            tmp_path, [self._line("user")],
            {"subagents/agent-x.jsonl": [shared], "subagents/agent-y.jsonl": [shared]},
        )
        assert "session $12.00" in statusline.run(stdin_payload(transcript))

    def test_entries_without_ids_are_never_mistaken_for_duplicates(self, tmp_path, home):
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user"), self._line("a", usage=self.USAGE)],
            {"subagents/agent-x.jsonl": [self._line("a", usage=self.USAGE)]},
        )
        assert "session $24.00" in statusline.run(stdin_payload(transcript))

    def test_precision_differences_do_not_drop_in_turn_work(self, tmp_path, home):
        # Text comparison would exclude this: '.' sorts before 'Z'.
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user", timestamp="2026-07-25T10:00:00Z")],
            {"subagents/agent-x.jsonl": [
                self._line("a", "m2", "claude-sonnet-5", self.USAGE, timestamp="2026-07-25T10:00:00.500Z")]},
        )
        assert "turn $12.00" in statusline.run(stdin_payload(transcript))

    def test_offset_spelling_differences_do_not_drop_in_turn_work(self, tmp_path, home):
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user", timestamp="2026-07-25T10:00:00Z")],
            {"subagents/agent-x.jsonl": [
                self._line("a", "m2", "claude-sonnet-5", self.USAGE, timestamp="2026-07-25T10:00:01+00:00")]},
        )
        assert "turn $12.00" in statusline.run(stdin_payload(transcript))

    def test_unparseable_stamps_fall_back_to_text_comparison(self, tmp_path, home):
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user", timestamp="stamp-a")],
            {"subagents/agent-x.jsonl": [
                self._line("a", "m2", "claude-sonnet-5", self.USAGE, timestamp="stamp-b")]},
        )
        assert "turn $12.00" in statusline.run(stdin_payload(transcript))

    def test_mixed_naive_and_aware_stamps_fall_back_to_text_comparison(self, tmp_path, home):
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user", timestamp="2026-07-25T10:00:00")],
            {"subagents/agent-x.jsonl": [
                self._line("a", "m2", "claude-sonnet-5", self.USAGE, timestamp="2026-07-25T10:05:00Z")]},
        )
        assert "turn $12.00" in statusline.run(stdin_payload(transcript))

    def test_a_last_prompt_without_a_stamp_keeps_agent_work_session_only(self, tmp_path, home):
        # The boundary is the LAST prompt; an earlier stamped prompt cannot stand in for it.
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user", timestamp="2026-07-25T10:00:00Z"), self._line("user")],
            {"subagents/agent-x.jsonl": [
                self._line("a", "m2", "claude-sonnet-5", self.USAGE, timestamp="2026-07-25T10:05:00Z")]},
        )
        line = statusline.run(stdin_payload(transcript))
        assert "turn $0.0000" in line and "session $12.00" in line

    def test_an_unreadable_agent_transcript_does_not_break_the_line(self, tmp_path, home):
        write_config(home, TIERED)
        transcript = self._session(
            tmp_path,
            [self._line("user"), self._line("a", "m1", "claude-sonnet-5", self.USAGE)],
            {"subagents/agent-x.jsonl": ["{not json", "", "null"]},
        )
        assert "session $12.00" in statusline.run(stdin_payload(transcript))

    def test_a_session_directory_that_is_a_file_is_ignored(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(self._line("user") + "\n")
        (tmp_path / "session").write_text("not a directory")
        assert statusline.subagent_transcripts(transcript) == []


class TestSavingsNeedMaterialRouting:
    """A model the session barely touched cannot be the yardstick for all of it.

    Regression for the second time this bug appeared. ADR-0009 stopped a configured model
    being used as the baseline; reading subagent transcripts (ADR-0010) then let one trivial
    delegation put a second model in the session, and the whole session was re-priced at its
    rates — reproducing the same fabricated "saved 50%".
    """

    BIG = {"input_tokens": 10_000_000, "output_tokens": 10_000_000}
    TINY = {"input_tokens": 1_000, "output_tokens": 1_000}

    def _totals(self, entries, config=None):
        return statusline.summarise(entries, TIERED, config or {}, last_user_line=0)

    def test_an_incidental_touch_on_a_dearer_model_is_not_a_baseline(self):
        result = self._totals([
            {"model": "claude-sonnet-5", "usage": self.BIG, "line": 1},
            {"model": "claude-fable-5", "usage": self.TINY, "line": 2},
        ])
        assert result["baseline"] == 0.0
        assert statusline._segment_saved(result, {}) is None

    def test_saved_still_cannot_equal_the_session_cost(self):
        # claude-fable-5 is exactly 2x claude-sonnet-5, the ratio that manufactures 50%.
        result = self._totals([
            {"model": "claude-sonnet-5", "usage": self.BIG, "line": 1},
            {"model": "claude-fable-5", "usage": self.TINY, "line": 2},
        ])
        assert result["baseline"] - result["session"] != pytest.approx(result["session"])

    def test_a_real_share_of_delegated_work_still_reports(self):
        result = self._totals([
            {"model": "claude-sonnet-5", "usage": self.BIG, "line": 1},
            {"model": "claude-fable-5", "usage": self.BIG, "line": 2},
        ])
        assert result["baseline"] > result["session"] > 0
        assert "saved" in statusline._segment_saved(result, {})

    def test_routing_off_means_nothing_was_saved(self):
        entries = [
            {"model": "claude-sonnet-5", "usage": self.BIG, "line": 1},
            {"model": "claude-fable-5", "usage": self.BIG, "line": 2},
        ]
        result = self._totals(entries, {"routing": {"enabled": False}})
        assert result["baseline"] == 0.0

    def test_the_line_never_claims_savings_and_routing_off_at_once(self, tmp_path, home):
        write_config(home, TIERED)
        config = json.loads((home / "config.json").read_text())
        config["routing"] = {"enabled": False}
        (home / "config.json").write_text(json.dumps(config))
        lines = [
            transcript_line("user"),
            transcript_line("assistant", msg_id="a", model="claude-sonnet-5", usage=self.BIG),
            transcript_line("assistant", msg_id="b", model="claude-fable-5", usage=self.BIG),
        ]
        line = statusline.run(stdin_payload(write_transcript(tmp_path, lines)))
        assert "routing off" in line and "saved" not in line

    def test_an_explicit_baseline_cannot_bypass_the_share_test(self):
        result = self._totals(
            [{"model": "claude-sonnet-5", "usage": self.BIG, "line": 1},
             {"model": "claude-fable-5", "usage": self.TINY, "line": 2}],
            {"statusline": {"savings_baseline": "claude-fable-5"}},
        )
        assert result["baseline"] == 0.0

    def test_an_override_never_run_cannot_become_the_yardstick(self):
        # Two material models pass the gates, but the override names a third the session
        # never touched; the yardstick must stay one of the models that actually ran.
        result = self._totals(
            [{"model": "claude-sonnet-5", "usage": self.BIG, "line": 1},
             {"model": "claude-sonnet-4-6", "usage": self.BIG, "line": 2}],
            {"statusline": {"savings_baseline": "claude-fable-5"}},
        )
        assert result["baseline_model"] == "claude-sonnet-4-6"
