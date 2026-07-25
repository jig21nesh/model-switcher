import json
from pathlib import Path

import pytest

import cli

PRICING = {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_write_1h": 10.0, "cache_read": 0.5}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_SWITCHER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def nested_home(tmp_path, monkeypatch):
    """An install whose parent is this test's own directory, not pytest's shared run dir.

    `status` looks for agents/ and settings.json beside the install, so a flat tmp_path home
    would read and write in a directory every other test shares.
    """
    install = tmp_path / ".claude" / "model-switcher"
    install.mkdir(parents=True)
    monkeypatch.setenv("MODEL_SWITCHER_HOME", str(install))
    return install


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

    def test_shows_matched_learned_terms(self, home, capsys, tmp_path):
        self._configure(home)
        (home / "classifier.json").write_text(
            json.dumps({"schema_version": 1, "scoring": {"terms": {"ensure": 1.2}, "max_adjustment": 3.0}})
        )
        cli.main(["explain", "--transcripts", str(tmp_path / "none"), "ensure the pipeline works"])
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


CLASSIFIER = {
    "schema_version": 1,
    "generated_at": "2026-07-25T03:39:15+00:00",
    "generator": "model-switcher/analyze_history",
    "corpus": {"sessions": 105, "prompts": 2143, "heavy": 544, "light": 1599},
    "scoring": {"max_adjustment": 3.0, "terms": {"naplan": -0.9, "migrate": 0.8}},
}


def write_classifier(directory, terms=None, name="classifier.json"):
    data = json.loads(json.dumps(CLASSIFIER))
    if terms is not None:
        data["scoring"]["terms"] = terms
    path = directory / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_transcripts(root, prompts_by_project):
    """One transcript per project, every prompt observable enough to have been trained on."""
    for project, prompts in prompts_by_project.items():
        directory = root / project
        directory.mkdir(parents=True, exist_ok=True)
        records = []
        for text in prompts:
            records.append({"type": "user", "sessionId": project, "message": {"content": text}})
            records.append({
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Read"}] * 14,
                            "usage": {"output_tokens": 500}},
            })
        (directory / f"{project}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
        )
    return root


class TestExplainDecisionBoundary:
    def _configure(self, home, threshold=5):
        (home / "config.json").write_text(
            json.dumps({"models": {"complex": "fable", "simple": "sonnet"},
                        "complexity": {"threshold": threshold}})
        )

    def test_reports_how_close_an_in_session_prompt_was(self, home, capsys, tmp_path):
        self._configure(home)
        cli.main(["explain", "--transcripts", str(tmp_path / "none"), "add a retry to the api client"])
        out = capsys.readouterr().out
        assert "decision boundary" in out and "short of the COMPLEX threshold (5)" in out
        assert "what would flip it" in out

    def test_reports_what_carried_a_routed_prompt(self, home, capsys, tmp_path):
        self._configure(home)
        cli.main([
            "explain", "--transcripts", str(tmp_path / "none"),
            "refactor the auth module and migrate the schema across the whole codebase",
        ])
        out = capsys.readouterr().out
        assert "what carried it there" in out and "without task verbs" in out

    def test_keeps_the_existing_output_and_the_ladder(self, home, capsys, tmp_path):
        self._configure(home)
        cli.main(["explain", "--transcripts", str(tmp_path / "none"), "refactor the auth module"])
        out = capsys.readouterr().out
        assert out.index("built-in score") < out.index("decision boundary") < out.index("routing ladder")

    def test_marks_a_term_that_only_one_project_ever_used(self, home, capsys, tmp_path):
        self._configure(home)
        write_classifier(home)
        root = write_transcripts(tmp_path / "projects", {
            "project-alpha": ["naplan results for the year", "migrate the schema"],
            "project-beta": ["migrate the database"],
        })
        cli.main(["explain", "--transcripts", str(root), "tidy the naplan report"])
        out = capsys.readouterr().out
        assert 'learned term "naplan"' in out and "topical: seen in only one project" in out

    def test_does_not_mark_a_term_seen_in_two_projects(self, home, capsys, tmp_path):
        self._configure(home)
        write_classifier(home)
        root = write_transcripts(tmp_path / "projects", {
            "project-alpha": ["migrate the schema"],
            "project-beta": ["migrate the database"],
        })
        cli.main(["explain", "--transcripts", str(root), "migrate the users"])
        assert "topical" not in capsys.readouterr().out

    def test_no_classifier_means_no_transcript_reading(self, home, capsys, tmp_path, monkeypatch):
        self._configure(home)
        write_classifier(home)
        monkeypatch.setattr(cli.classifier_report, "attribute", _fail_if_called)
        cli.main(["explain", "--no-classifier", "tidy the naplan report"])
        assert "topical" not in capsys.readouterr().out

    def test_a_missing_transcripts_directory_is_not_an_error(self, home, capsys, tmp_path):
        self._configure(home)
        write_classifier(home)
        assert cli.main(["explain", "--transcripts", str(tmp_path / "gone"), "tidy the naplan report"]) == 0
        assert "topical" not in capsys.readouterr().out

    def test_a_prompt_matching_no_learned_term_reads_no_transcripts(self, home, monkeypatch, tmp_path):
        self._configure(home)
        write_classifier(home)
        monkeypatch.setattr(cli.classifier_report, "attribute", _fail_if_called)
        assert cli.main(["explain", "--transcripts", str(tmp_path), "tidy the module"]) == 0


def _fail_if_called(*args, **kwargs):
    raise AssertionError("transcripts must not be read for this prompt")


class TestClassifierCommand:
    def test_reports_the_installed_artifact_by_default(self, home, capsys, tmp_path):
        write_classifier(home)
        root = write_transcripts(tmp_path / "projects", {"project-alpha": ["migrate the naplan schema"]})
        assert cli.main(["classifier", "--transcripts", str(root)]) == 0
        out = capsys.readouterr().out
        assert str(home / "classifier.json") in out
        assert "2,143 prompts from 105 sessions" in out and "project-alpha" in out

    def test_reads_an_explicit_artifact_path(self, home, capsys, tmp_path):
        elsewhere = write_classifier(tmp_path, name="classifier.candidate.json")
        assert cli.main([
            "classifier", "--config", str(elsewhere), "--transcripts", str(tmp_path / "none"),
        ]) == 0
        assert "classifier.candidate.json" in capsys.readouterr().out

    def test_says_how_to_create_a_missing_one(self, home, capsys, tmp_path):
        assert cli.main(["classifier", "--transcripts", str(tmp_path / "none")]) == 2
        assert "learn --apply" in capsys.readouterr().err

    @pytest.mark.parametrize("body", ["{not json", '"a string"', "[1, 2]", ""])
    def test_survives_an_unusable_artifact(self, home, capsys, body, tmp_path):
        (home / "classifier.json").write_text(body, encoding="utf-8")
        assert cli.main(["classifier", "--transcripts", str(tmp_path / "none")]) == 2
        assert capsys.readouterr().err.strip()

    def test_reports_an_artifact_with_no_usable_terms(self, home, capsys, tmp_path):
        write_classifier(home, terms={})
        assert cli.main(["classifier", "--transcripts", str(tmp_path / "none")]) == 2
        assert "nothing here is in effect" in capsys.readouterr().out

    def test_flags_single_project_vocabulary(self, home, capsys, tmp_path):
        write_classifier(home)
        root = write_transcripts(tmp_path / "projects", {
            "project-alpha": ["naplan results", "migrate the schema"],
            "project-beta": ["migrate the database"],
        })
        cli.main(["classifier", "--transcripts", str(root)])
        out = capsys.readouterr().out
        assert "one project only" in out and "naplan -0.90" in out


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


TUNE_CONFIG = {
    "models": {"complex": "fable", "simple": "sonnet"},
    "complexity": {"threshold": 5},
    "pricing_usd_per_mtok": {
        "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
        "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
    },
}


class TestTuneCommand:
    def _transcripts(self, tmp_path, sessions=3, name="projects"):
        directory = tmp_path / name / "proj"
        directory.mkdir(parents=True, exist_ok=True)
        for s in range(sessions):
            records = []
            for i in range(4):
                heavy = i % 2 == 0
                text = "refactor the auth module and migrate the schema" if heavy else "rename it"
                records.append({"type": "user", "sessionId": f"{name}-s{s}", "message": {"content": text}})
                records.append({"type": "assistant", "message": {
                    "id": f"m{s}-{i}", "model": "claude-sonnet-5",
                    "content": [{"type": "tool_use", "name": "Edit"}] * (14 if heavy else 1),
                    "usage": {"input_tokens": 100, "output_tokens": 900},
                }})
            (directory / f"t{s}.jsonl").write_text(
                "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
            )
        return directory

    def test_reports_the_calibration_and_the_sweep(self, home, tmp_path, capsys):
        (home / "config.json").write_text(json.dumps(TUNE_CONFIG))
        assert cli.main(["tune", "--transcripts", str(self._transcripts(tmp_path))]) == 0
        out = capsys.readouterr().out
        assert "became real work" in out and "<- current threshold (5)" in out
        assert "$/1k prompts" in out and "ESTIMATE, not a quote" in out

    def test_announces_what_it_reads_before_reading_it(self, home, tmp_path, capsys):
        (home / "config.json").write_text(json.dumps(TUNE_CONFIG))
        directory = self._transcripts(tmp_path)
        cli.main(["tune", "--transcripts", str(directory)])
        out = capsys.readouterr().out
        assert str(directory) in out and "nothing leaves this machine" in out

    def test_reads_every_transcript_directory_it_is_given(self, home, tmp_path, capsys):
        (home / "config.json").write_text(json.dumps(TUNE_CONFIG))
        first = self._transcripts(tmp_path, sessions=2, name="a")
        second = self._transcripts(tmp_path, sessions=3, name="b")
        cli.main(["tune", "--transcripts", str(first), "--transcripts", str(second)])
        assert "20 usable prompts from 5 sessions" in capsys.readouterr().out

    def test_honours_the_session_cap(self, home, tmp_path, capsys):
        (home / "config.json").write_text(json.dumps(TUNE_CONFIG))
        cli.main(["tune", "--transcripts", str(self._transcripts(tmp_path, sessions=4)), "--max-sessions", "1"])
        assert "4 usable prompts from 1 sessions" in capsys.readouterr().out

    def test_reads_an_explicit_config_path(self, home, tmp_path, capsys):
        elsewhere = tmp_path / "other.json"
        elsewhere.write_text(json.dumps(dict(TUNE_CONFIG, complexity={"threshold": 7})))
        code = cli.main([
            "tune", "--config", str(elsewhere), "--transcripts", str(self._transcripts(tmp_path)),
        ])
        assert code == 0 and "<- current threshold (7)" in capsys.readouterr().out

    def test_reports_an_empty_corpus_rather_than_inventing_a_table(self, home, tmp_path, capsys):
        (home / "config.json").write_text(json.dumps(TUNE_CONFIG))
        assert cli.main(["tune", "--transcripts", str(tmp_path / "absent")]) == 1
        captured = capsys.readouterr()
        assert "no usable prompts" in captured.err
        assert "became real work" not in captured.out

    def test_survives_a_corpus_of_nothing_but_malformed_lines(self, home, tmp_path, capsys):
        (home / "config.json").write_text(json.dumps(TUNE_CONFIG))
        directory = tmp_path / "junk" / "proj"
        directory.mkdir(parents=True)
        (directory / "t.jsonl").write_text("{ not json\n[1,2]\nnull\n\x00\n", encoding="utf-8")
        assert cli.main(["tune", "--transcripts", str(directory)]) == 1
        assert "no usable prompts" in capsys.readouterr().err

    def test_reports_a_missing_config_rather_than_crashing(self, home, capsys):
        assert cli.main(["tune"]) == 2
        assert "run ./install.sh first" in capsys.readouterr().err

    @pytest.mark.parametrize("body", ["{not json", '"a string"', "[1, 2]", ""])
    def test_survives_an_unusable_config(self, home, capsys, body):
        (home / "config.json").write_text(body)
        assert cli.main(["tune"]) == 2
        assert capsys.readouterr().err.strip()


class TestStatusCommand:
    def _install(self, home, config, agent=None, settings=None):
        (home / "config.json").write_text(json.dumps(config))
        agents = home.parent / "agents"
        agents.mkdir(parents=True, exist_ok=True)
        if agent:
            (agents / agent).write_text("agent")
        if settings is not None:
            (home.parent / "settings.json").write_text(json.dumps({"model": settings}))

    HEALTHY = {
        "models": {"complex": "fable", "simple": "sonnet"},
        "pricing_usd_per_mtok": {
            "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_write": 12.5, "cache_read": 1.0},
            "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
        },
        "routing": {"enabled": False},
    }

    def test_reports_configuration_and_the_ladder(self, nested_home, tmp_path, capsys):
        self._install(nested_home, self.HEALTHY, agent="heavy-task-fable.md", settings="sonnet")
        code = cli.main(["status", "--transcripts", str(tmp_path / "none")])
        out = capsys.readouterr().out
        assert code == 0
        assert "session model   sonnet" in out and "complex=fable" in out
        assert "routing ladder" in out and "heavy-task-fable" in out
        assert "nothing wrong found" in out

    def test_exits_non_zero_when_something_is_broken(self, nested_home, tmp_path, capsys):
        self._install(nested_home, self.HEALTHY, settings="sonnet")  # no agent file
        code = cli.main(["status", "--transcripts", str(tmp_path / "none")])
        assert code == 1 and "BROKEN" in capsys.readouterr().out

    def test_advice_does_not_make_it_fail(self, nested_home, tmp_path, capsys):
        self._install(nested_home, self.HEALTHY, agent="heavy-task-fable.md", settings="opus")
        code = cli.main(["status", "--transcripts", str(tmp_path / "none")])
        assert code == 0 and "note" in capsys.readouterr().out

    def test_reports_a_missing_config_rather_than_crashing(self, nested_home, capsys):
        assert cli.main(["status"]) == 2
        assert "run ./install.sh first" in capsys.readouterr().err

    @pytest.mark.parametrize("body", ["{not json", '"a string"', "[1, 2]"])
    def test_survives_an_unusable_config(self, nested_home, capsys, body):
        (nested_home / "config.json").write_text(body)
        assert cli.main(["status"]) == 2
        assert capsys.readouterr().err.strip()
