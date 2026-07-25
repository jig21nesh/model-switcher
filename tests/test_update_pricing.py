import io
import json
import urllib.error

import pytest

import update_pricing

GOOD_RATES = {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5}
SOURCE = {
    "schema_version": 1,
    "updated": "2026-07-25",
    "source": "https://platform.claude.com/docs/en/about-claude/pricing",
    "notes": ["USD per million tokens."],
    "models": {"claude-opus-5": dict(GOOD_RATES)},
}


def write_config(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class TestValidateSource:
    def test_accepts_a_well_formed_table(self):
        assert update_pricing.validate_source(SOURCE)["claude-opus-5"]["input"] == 5.0

    def test_keeps_the_optional_fast_block(self):
        payload = {**SOURCE, "models": {"m": {**GOOD_RATES, "fast": dict(GOOD_RATES)}}}
        assert update_pricing.validate_source(payload)["m"]["fast"]["output"] == 25.0

    def test_drops_unknown_keys_rather_than_passing_them_through(self):
        payload = {**SOURCE, "models": {"m": {**GOOD_RATES, "surprise": "value"}}}
        assert "surprise" not in update_pricing.validate_source(payload)["m"]

    @pytest.mark.parametrize(
        "payload",
        [
            "not-an-object",
            {"models": {"m": GOOD_RATES}},
            {"schema_version": 99, "models": {"m": GOOD_RATES}},
            {"schema_version": 1},
            {"schema_version": 1, "models": {}},
            {"schema_version": 1, "models": "nope"},
        ],
    )
    def test_rejects_a_structurally_wrong_table(self, payload):
        with pytest.raises(update_pricing.PricingError):
            update_pricing.validate_source(payload)

    @pytest.mark.parametrize("model", ["../../etc/passwd", "model id", "x" * 65, "rm -rf /", ""])
    def test_rejects_implausible_model_ids(self, model):
        with pytest.raises(update_pricing.PricingError):
            update_pricing.validate_source({**SOURCE, "models": {model: GOOD_RATES}})

    @pytest.mark.parametrize(
        "bad", [True, -1, float("inf"), float("nan"), "5.0", None, 1e9, [5.0], {"nested": 1}]
    )
    def test_rejects_rates_that_are_not_usable_numbers(self, bad):
        with pytest.raises(update_pricing.PricingError):
            update_pricing.validate_source({**SOURCE, "models": {"m": {**GOOD_RATES, "input": bad}}})

    def test_rejects_a_missing_required_rate(self):
        rates = {key: value for key, value in GOOD_RATES.items() if key != "cache_read"}
        with pytest.raises(update_pricing.PricingError, match="cache_read"):
            update_pricing.validate_source({**SOURCE, "models": {"m": rates}})

    def test_rejects_a_poisoned_nested_fast_block(self):
        payload = {**SOURCE, "models": {"m": {**GOOD_RATES, "fast": {**GOOD_RATES, "output": -1}}}}
        with pytest.raises(update_pricing.PricingError, match="fast"):
            update_pricing.validate_source(payload)

    def test_rejects_a_rate_block_that_is_not_an_object(self):
        with pytest.raises(update_pricing.PricingError):
            update_pricing.validate_source({**SOURCE, "models": {"m": [1, 2, 3]}})


class TestFetchSource:
    def test_refuses_plain_http(self):
        with pytest.raises(update_pricing.PricingError, match="non-HTTPS"):
            update_pricing.fetch_source("http://example.com/pricing.json")

    def test_refuses_a_file_url(self):
        with pytest.raises(update_pricing.PricingError, match="non-HTTPS"):
            update_pricing.fetch_source("file:///etc/passwd")

    def test_reads_an_https_response(self, monkeypatch):
        monkeypatch.setattr(update_pricing.urllib.request, "urlopen", _fake_urlopen(json.dumps(SOURCE).encode()))
        assert update_pricing.fetch_source("https://example.com/p.json")["schema_version"] == 1

    def test_rejects_an_oversized_response(self, monkeypatch):
        payload = b"x" * (update_pricing.MAX_SOURCE_BYTES + 1)
        monkeypatch.setattr(update_pricing.urllib.request, "urlopen", _fake_urlopen(payload))
        with pytest.raises(update_pricing.PricingError, match="larger than"):
            update_pricing.fetch_source("https://example.com/p.json")

    def test_rejects_a_non_json_response(self, monkeypatch):
        monkeypatch.setattr(update_pricing.urllib.request, "urlopen", _fake_urlopen(b"<html>404</html>"))
        with pytest.raises(update_pricing.PricingError, match="not valid JSON"):
            update_pricing.fetch_source("https://example.com/p.json")

    def test_rejects_undecodable_bytes(self, monkeypatch):
        monkeypatch.setattr(update_pricing.urllib.request, "urlopen", _fake_urlopen(b"\xff\xfe\x00bad"))
        with pytest.raises(update_pricing.PricingError):
            update_pricing.fetch_source("https://example.com/p.json")

    def test_surfaces_a_network_failure(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise urllib.error.URLError("unreachable")

        monkeypatch.setattr(update_pricing.urllib.request, "urlopen", boom)
        with pytest.raises(update_pricing.PricingError, match="cannot fetch"):
            update_pricing.fetch_source("https://example.com/p.json")


def _fake_urlopen(payload: bytes):
    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    return lambda *_args, **_kwargs: _Response(payload)


class TestReadSource:
    def test_reads_the_bundled_table(self, tmp_path):
        path = tmp_path / "pricing.json"
        path.write_text(json.dumps(SOURCE), encoding="utf-8")
        assert update_pricing.read_source(path)["schema_version"] == 1

    def test_reports_a_missing_file(self, tmp_path):
        with pytest.raises(update_pricing.PricingError, match="cannot read"):
            update_pricing.read_source(tmp_path / "absent.json")


class TestDiff:
    def test_reports_a_model_the_config_lacks(self):
        added, changed = update_pricing.diff({}, {"m": dict(GOOD_RATES)})
        assert added == ["m"] and changed == []

    def test_reports_a_changed_rate(self):
        added, changed = update_pricing.diff({"m": {**GOOD_RATES, "input": 4.0}}, {"m": dict(GOOD_RATES)})
        assert added == [] and changed == [("m", "input", 4.0, 5.0)]

    def test_reports_an_absent_optional_rate_as_a_change(self):
        current = {key: value for key, value in GOOD_RATES.items() if key != "cache_write_1h"}
        _added, changed = update_pricing.diff({"m": current}, {"m": dict(GOOD_RATES)})
        assert changed == [("m", "cache_write_1h", None, 10.0)]

    def test_finds_nothing_when_already_current(self):
        assert update_pricing.diff({"m": dict(GOOD_RATES)}, {"m": dict(GOOD_RATES)}) == ([], [])

    def test_treats_a_boolean_rate_as_a_change_not_a_match(self):
        _added, changed = update_pricing.diff({"m": {**GOOD_RATES, "input": True}}, {"m": dict(GOOD_RATES)})
        assert ("m", "input", True, 5.0) in changed

    def test_tolerates_a_config_whose_pricing_is_not_an_object(self):
        added, _changed = update_pricing.diff("garbage", {"m": dict(GOOD_RATES)})
        assert added == ["m"]

    def test_compares_nested_fast_rates(self):
        source = {"m": {**GOOD_RATES, "fast": dict(GOOD_RATES)}}
        _added, changed = update_pricing.diff({"m": dict(GOOD_RATES)}, source)
        assert ("m", "fast.input", None, 5.0) in changed


class TestApply:
    def test_merges_source_models_and_metadata(self):
        config = update_pricing.apply({"models": {"complex": "fable"}}, {"m": dict(GOOD_RATES)}, SOURCE)
        assert config["pricing_usd_per_mtok"]["m"]["input"] == 5.0
        assert config["models"] == {"complex": "fable"}, "unrelated config must survive"
        assert config["pricing_updated"] == "2026-07-25"
        assert config["pricing_notes"] == "USD per million tokens."

    def test_keeps_models_the_source_does_not_know(self):
        config = {"pricing_usd_per_mtok": {"my-own-model": dict(GOOD_RATES)}}
        merged = update_pricing.apply(config, {"m": dict(GOOD_RATES)}, SOURCE)["pricing_usd_per_mtok"]
        assert "my-own-model" in merged and "m" in merged

    def test_survives_a_config_whose_pricing_is_the_wrong_type(self):
        config = update_pricing.apply({"pricing_usd_per_mtok": "garbage"}, {"m": dict(GOOD_RATES)}, SOURCE)
        assert config["pricing_usd_per_mtok"] == {"m": dict(GOOD_RATES)}


class TestMain:
    def test_check_only_reports_drift_without_writing(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(update_pricing, "BUNDLED", _bundled(tmp_path))
        config = write_config(tmp_path, {"pricing_usd_per_mtok": {}})
        before = config.read_text(encoding="utf-8")
        code = update_pricing.main(["--config", str(config), "--offline", "--bundled", str(_bundled(tmp_path))])
        assert code == 1
        assert "Re-run with --yes" in capsys.readouterr().out
        assert config.read_text(encoding="utf-8") == before

    def test_apply_writes_and_backs_up(self, tmp_path, capsys):
        config = write_config(tmp_path, {"pricing_usd_per_mtok": {}, "models": {"complex": "fable"}})
        code = update_pricing.main(
            ["--config", str(config), "--offline", "--bundled", str(_bundled(tmp_path)), "--yes"]
        )
        assert code == 0 and "applied to" in capsys.readouterr().out
        written = json.loads(config.read_text(encoding="utf-8"))
        assert written["pricing_usd_per_mtok"]["claude-opus-5"]["cache_write_1h"] == 10.0
        assert written["models"] == {"complex": "fable"}
        assert (tmp_path / "config.json.pricing.bak").exists()

    def test_reports_nothing_to_do_when_current(self, tmp_path, capsys):
        config = write_config(tmp_path, {"pricing_usd_per_mtok": {"claude-opus-5": dict(GOOD_RATES)}})
        code = update_pricing.main(["--config", str(config), "--offline", "--bundled", str(_bundled(tmp_path))])
        assert code == 0 and "up to date" in capsys.readouterr().out

    def test_a_poisoned_source_never_touches_the_config(self, tmp_path, capsys):
        bundled = tmp_path / "poisoned.json"
        bundled.write_text(json.dumps({"schema_version": 1, "models": {"m": {**GOOD_RATES, "input": -5}}}))
        config = write_config(tmp_path, {"pricing_usd_per_mtok": {}})
        before = config.read_text(encoding="utf-8")
        code = update_pricing.main(["--config", str(config), "--offline", "--bundled", str(bundled), "--yes"])
        assert code == 2 and "not a usable number" in capsys.readouterr().err
        assert config.read_text(encoding="utf-8") == before

    def test_creates_pricing_for_an_absent_config(self, tmp_path):
        config = tmp_path / "config.json"
        code = update_pricing.main(
            ["--config", str(config), "--offline", "--bundled", str(_bundled(tmp_path)), "--yes"]
        )
        assert code == 0 and json.loads(config.read_text(encoding="utf-8"))["pricing_usd_per_mtok"]

    def test_rejects_a_malformed_config(self, tmp_path, capsys):
        config = tmp_path / "config.json"
        config.write_text("{ not json", encoding="utf-8")
        code = update_pricing.main(["--config", str(config), "--offline", "--bundled", str(_bundled(tmp_path))])
        assert code == 2 and "cannot parse" in capsys.readouterr().err

    def test_rejects_a_config_that_is_not_an_object(self, tmp_path, capsys):
        config = tmp_path / "config.json"
        config.write_text("[1, 2, 3]", encoding="utf-8")
        code = update_pricing.main(["--config", str(config), "--offline", "--bundled", str(_bundled(tmp_path))])
        assert code == 2 and "JSON object" in capsys.readouterr().err

    def test_reports_an_unreachable_source(self, tmp_path, capsys, monkeypatch):
        def boom(*_args, **_kwargs):
            raise urllib.error.URLError("unreachable")

        monkeypatch.setattr(update_pricing.urllib.request, "urlopen", boom)
        config = write_config(tmp_path, {"pricing_usd_per_mtok": {}})
        code = update_pricing.main(["--config", str(config), "--source", "https://example.com/p.json"])
        assert code == 2 and "cannot fetch" in capsys.readouterr().err


def _bundled(tmp_path):
    path = tmp_path / "bundled-pricing.json"
    if not path.exists():
        path.write_text(json.dumps(SOURCE), encoding="utf-8")
    return path


def test_the_shipped_table_and_example_config_agree():
    """The starter config must not ship already stale against the maintained table."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    source = update_pricing.validate_source(json.loads((root / "config" / "pricing.json").read_text()))
    example = json.loads((root / "config" / "config.example.json").read_text())
    added, changed = update_pricing.diff(example["pricing_usd_per_mtok"], source)
    assert (added, changed) == ([], [])
