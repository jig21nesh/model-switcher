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
        return json.dumps({"type": "user", "isMeta": is_meta, "isSidechain": sidechain, "message": {}})
    return json.dumps(
        {"type": "assistant", "isSidechain": sidechain, "message": {"id": msg_id, "model": model, "usage": usage}}
    )


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
