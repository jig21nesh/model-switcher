import json
from pathlib import Path

import pytest

import cli

PRICING = {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_SWITCHER_HOME", str(tmp_path))
    return tmp_path


def write_config(home, pricing=None):
    path = home / "config.json"
    path.write_text(json.dumps({"pricing_usd_per_mtok": pricing or {"claude-opus-5": dict(PRICING)}}))
    return path


class TestPaths:
    def test_home_follows_the_environment_override(self, home):
        assert cli.home_dir() == home
        assert cli.config_path() == home / "config.json"

    def test_home_defaults_under_the_users_claude_directory(self, monkeypatch):
        monkeypatch.delenv("MODEL_SWITCHER_HOME", raising=False)
        assert cli.home_dir() == Path.home() / ".claude" / "model-switcher"


class TestPricingCommand:
    def test_checks_the_installed_config_by_default(self, home, monkeypatch):
        config = write_config(home)
        seen = {}
        monkeypatch.setattr(cli.update_pricing, "main", lambda argv: seen.setdefault("argv", argv) and 0)
        cli.main(["pricing", "--offline"])
        assert seen["argv"][seen["argv"].index("--config") + 1] == str(config)

    def test_reports_a_missing_install_rather_than_crashing(self, home, capsys):
        assert cli.main(["pricing", "--offline"]) == 2
        assert "run ./install.sh first" in capsys.readouterr().err

    def test_accepts_an_explicit_config_path(self, tmp_path, capsys):
        config = tmp_path / "elsewhere.json"
        config.write_text(json.dumps({"pricing_usd_per_mtok": {}}))
        assert cli.main(["pricing", "--offline", "--config", str(config)]) == 1
        assert "Re-run with --yes" in capsys.readouterr().out

    def test_applies_when_asked(self, tmp_path):
        config = tmp_path / "config.json"
        config.write_text(json.dumps({"pricing_usd_per_mtok": {}, "models": {"complex": "fable"}}))
        assert cli.main(["pricing", "--offline", "--config", str(config), "--yes"]) == 0
        written = json.loads(config.read_text(encoding="utf-8"))
        assert written["pricing_usd_per_mtok"]["claude-opus-5"]["cache_write_1h"] == 10.0
        assert written["models"] == {"complex": "fable"}, "unrelated config must survive"

    def test_forwards_a_custom_source(self, tmp_path, monkeypatch):
        seen = {}

        def fake_main(argv):
            seen["argv"] = argv
            return 0

        monkeypatch.setattr(cli.update_pricing, "main", fake_main)
        config = tmp_path / "config.json"
        config.write_text("{}")
        cli.main(["pricing", "--config", str(config), "--source", "https://example.com/p.json", "--yes"])
        assert "--source" in seen["argv"] and "https://example.com/p.json" in seen["argv"]
        assert "--yes" in seen["argv"] and "--offline" not in seen["argv"]

    def test_reports_up_to_date_against_the_shipped_table(self, tmp_path, capsys):
        root = Path(__file__).resolve().parent.parent
        example = json.loads((root / "config" / "config.example.json").read_text())
        config = tmp_path / "config.json"
        config.write_text(json.dumps(example))
        assert cli.main(["pricing", "--offline", "--config", str(config)]) == 0
        assert "up to date" in capsys.readouterr().out


class TestParser:
    def test_requires_a_subcommand(self):
        with pytest.raises(SystemExit):
            cli.main([])

    def test_rejects_an_unknown_subcommand(self):
        with pytest.raises(SystemExit):
            cli.main(["teleport"])


class TestConfiguredThreshold:
    def test_reads_the_users_threshold(self, home):
        (home / "config.json").write_text(json.dumps({"complexity": {"threshold": 7}}))
        assert cli.configured_threshold() == 7.0

    @pytest.mark.parametrize("config", [
        {}, {"complexity": {}}, {"complexity": "nope"},
        {"complexity": {"threshold": True}}, {"complexity": {"threshold": "5"}},
        {"complexity": {"threshold": None}},
    ])
    def test_falls_back_to_the_default_for_anything_unusable(self, home, config):
        (home / "config.json").write_text(json.dumps(config))
        assert cli.configured_threshold() == 5.0

    def test_falls_back_when_there_is_no_config(self, home):
        assert cli.configured_threshold() == 5.0

    def test_falls_back_for_a_malformed_config(self, home):
        (home / "config.json").write_text("{ not json")
        assert cli.configured_threshold() == 5.0


class TestLearnCommand:
    def test_reports_a_missing_install(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MODEL_SWITCHER_HOME", str(tmp_path / "absent"))
        assert cli.main(["learn"]) == 2
        assert "run ./install.sh first" in capsys.readouterr().err

    def test_forwards_the_install_home_and_a_timestamp(self, home, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli.analyze_history, "main", lambda argv: seen.setdefault("argv", argv) and 0)
        cli.main(["learn"])
        argv = seen["argv"]
        assert argv[argv.index("--home") + 1] == str(home)
        assert argv[argv.index("--generated-at") + 1].endswith("+00:00")

    def test_measures_against_the_configured_threshold(self, home, monkeypatch):
        (home / "config.json").write_text(json.dumps({"complexity": {"threshold": 8}}))
        seen = {}
        monkeypatch.setattr(cli.analyze_history, "main", lambda argv: seen.setdefault("argv", argv) and 0)
        cli.main(["learn"])
        assert seen["argv"][seen["argv"].index("--threshold") + 1] == "8.0"

    def test_an_explicit_threshold_wins(self, home, monkeypatch):
        (home / "config.json").write_text(json.dumps({"complexity": {"threshold": 8}}))
        seen = {}
        monkeypatch.setattr(cli.analyze_history, "main", lambda argv: seen.setdefault("argv", argv) and 0)
        cli.main(["learn", "--threshold", "3"])
        assert seen["argv"][seen["argv"].index("--threshold") + 1] == "3.0"

    def test_forwards_optional_flags(self, home, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(cli.analyze_history, "main", lambda argv: seen.setdefault("argv", argv) and 0)
        cli.main([
            "learn", "--apply", "--max-sessions", "5",
            "--transcripts", str(tmp_path / "a"), "--transcripts", str(tmp_path / "b"),
        ])
        argv = seen["argv"]
        assert "--apply" in argv
        assert argv[argv.index("--max-sessions") + 1] == "5"
        assert argv.count("--transcripts") == 2

    def test_omits_flags_that_were_not_asked_for(self, home, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli.analyze_history, "main", lambda argv: seen.setdefault("argv", argv) and 0)
        cli.main(["learn"])
        assert "--apply" not in seen["argv"] and "--max-sessions" not in seen["argv"]


class TestExplainCommand:
    def _configure(self, home, threshold=5):
        (home / "config.json").write_text(
            json.dumps({"models": {"complex": "fable", "simple": "sonnet"},
                        "complexity": {"threshold": threshold}})
        )

    def test_shows_signals_and_the_routing_verdict(self, home, capsys):
        self._configure(home)
        assert cli.main(["explain", "refactor", "the", "auth", "module"]) == 0
        out = capsys.readouterr().out
        assert "task verbs" in out and "heavy-task-fable" in out and "COMPLEX" in out

    def test_reports_an_in_session_prompt(self, home, capsys):
        self._configure(home)
        cli.main(["explain", "what does this function do?"])
        out = capsys.readouterr().out
        assert "answered in-session" in out and "capped to 2 by" in out

    def test_says_when_no_built_in_signal_matched(self, home, capsys):
        self._configure(home)
        cli.main(["explain", "hmm"])
        assert "no built-in signals matched" in capsys.readouterr().out

    def test_prompts_for_a_classifier_when_none_exists(self, home, capsys):
        self._configure(home)
        cli.main(["explain", "refactor the module"])
        assert "model-switcher learn" in capsys.readouterr().out

    def test_shows_matched_learned_terms(self, home, capsys):
        self._configure(home)
        (home / "classifier.json").write_text(
            json.dumps({"schema_version": 1, "scoring": {"terms": {"ensure": 1.2}, "max_adjustment": 3.0}})
        )
        cli.main(["explain", "ensure the pipeline works"])
        out = capsys.readouterr().out
        assert "learned terms" in out and "ensure +1.20" in out

    def test_no_classifier_flag_scores_with_built_ins_only(self, home, capsys):
        self._configure(home)
        (home / "classifier.json").write_text(
            json.dumps({"schema_version": 1, "scoring": {"terms": {"ensure": 1.2}}})
        )
        cli.main(["explain", "--no-classifier", "ensure the pipeline works"])
        assert "model-switcher learn" in capsys.readouterr().out

    def test_reports_the_middle_tier(self, home, capsys):
        (home / "config.json").write_text(json.dumps({
            "models": {"complex": "fable", "standard": "sonnet", "simple": "haiku"},
            "complexity": {"threshold": 5, "standard_threshold": 1},
        }))
        cli.main(["explain", "fix the api config"])
        out = capsys.readouterr().out
        assert "MODERATE" in out and "mid-task-sonnet" in out

    def test_rejects_an_empty_prompt(self, home, capsys):
        self._configure(home)
        assert cli.main(["explain", "   "]) == 2
        assert "nothing to score" in capsys.readouterr().err

    def test_truncates_a_very_long_prompt_in_the_echo(self, home, capsys):
        self._configure(home)
        cli.main(["explain", "refactor " * 200])
        assert "..." in capsys.readouterr().out


class TestUninstallCommand:
    def _installed(self, home):
        (home / "installed.json").write_text(json.dumps({"created_claude_md": False}))
        (home / "config.json").write_text("{}")
        return home

    def test_dry_run_changes_nothing(self, home, capsys, monkeypatch):
        self._installed(home)
        called = []
        monkeypatch.setattr(cli.uninstall_module, "main", lambda argv: called.append(argv) or 0)
        assert cli.main(["uninstall"]) == 1
        assert "Re-run with --yes" in capsys.readouterr().out
        assert called == [], "a dry run must not touch anything"

    def test_yes_performs_the_removal(self, home, monkeypatch):
        self._installed(home)
        called = []
        monkeypatch.setattr(cli.uninstall_module, "main", lambda argv: called.append(argv) or 0)
        assert cli.main(["uninstall", "--yes"]) == 0
        argv = called[0]
        assert argv[argv.index("--install-dir") + 1] == str(home)
        assert argv[argv.index("--claude-dir") + 1] == str(home.parent)

    def test_reports_when_nothing_is_installed(self, home, capsys):
        assert cli.main(["uninstall", "--yes"]) == 2
        assert "nothing installed" in capsys.readouterr().err

    def test_honours_an_explicit_claude_dir(self, home, tmp_path, monkeypatch):
        self._installed(home)
        called = []
        monkeypatch.setattr(cli.uninstall_module, "main", lambda argv: called.append(argv) or 0)
        cli.main(["uninstall", "--yes", "--claude-dir", str(tmp_path / "elsewhere")])
        argv = called[0]
        assert argv[argv.index("--claude-dir") + 1] == str(tmp_path / "elsewhere")


THREE_TIER = {
    "models": {"complex": "fable", "standard": "sonnet", "simple": "haiku"},
    "complexity": {"threshold": 5, "standard_threshold": 3},
}


class TestTiersCommand:
    def _write(self, home, config):
        path = home / "config.json"
        path.write_text(json.dumps(config))
        return path

    def test_shows_every_band_with_its_model_and_destination(self, home, capsys):
        self._write(home, THREE_TIER)
        assert cli.main(["tiers"]) == 0
        out = capsys.readouterr().out
        assert "3 tiers" in out
        assert "score < 3" in out and "haiku" in out and "answered in-session" in out
        assert "3 <= score < 5" in out and "mid-task-sonnet" in out
        assert "score >= 5" in out and "heavy-task-fable" in out

    def test_a_two_tier_config_says_how_to_add_the_middle_one(self, home, capsys):
        self._write(home, {"models": {"complex": "opus", "simple": "sonnet"}})
        assert cli.main(["tiers"]) == 0
        out = capsys.readouterr().out
        assert "2 tiers" in out and "models.standard" in out and "mid-task" not in out

    def test_reads_an_explicit_config_path(self, home, tmp_path, capsys):
        elsewhere = tmp_path / "other.json"
        elsewhere.write_text(json.dumps(THREE_TIER))
        assert cli.main(["tiers", "--config", str(elsewhere)]) == 0
        assert "mid-task-sonnet" in capsys.readouterr().out

    def test_reports_a_missing_config_rather_than_crashing(self, home, capsys):
        assert cli.main(["tiers"]) == 2
        assert "run ./install.sh first" in capsys.readouterr().err

    @pytest.mark.parametrize("body", ["{not json", '"a string"', "[1, 2]", ""])
    def test_survives_an_unusable_config(self, home, capsys, body):
        (home / "config.json").write_text(body)
        assert cli.main(["tiers"]) == 2
        assert capsys.readouterr().err.strip()

    def test_explain_ends_with_the_ladder_and_marks_where_the_prompt_landed(self, home, capsys):
        self._write(home, THREE_TIER)
        assert cli.main(["explain", "refactor the auth module and migrate the schema"]) == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        marked = [line for line in lines if line.startswith("  ->")]
        assert len(marked) == 1 and "heavy-task-fable" in marked[0]

    def test_explain_marks_the_in_session_band_for_a_simple_prompt(self, home, capsys):
        self._write(home, THREE_TIER)
        cli.main(["explain", "what does this do?"])
        marked = [line for line in capsys.readouterr().out.splitlines() if line.startswith("  ->")]
        assert len(marked) == 1 and "answered in-session" in marked[0]
