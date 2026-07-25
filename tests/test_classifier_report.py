import json

import pytest

import classifier_report as report
import complexity_router as router


def artifact(terms, **overrides):
    data = {
        "schema_version": 1,
        "generated_at": "2026-07-25T03:39:15+00:00",
        "generator": "model-switcher/analyze_history",
        "corpus": {"sessions": 105, "prompts": 2143, "heavy": 544, "light": 1599},
        "scoring": {"max_adjustment": 3.0, "min_session_support": 3, "terms": terms},
    }
    data.update(overrides)
    return data


def write_artifact(tmp_path, terms, name="classifier.json", **overrides):
    path = tmp_path / name
    path.write_text(json.dumps(artifact(terms, **overrides)), encoding="utf-8")
    return path


def transcript(directory, name, prompts, tools=14, output=500):
    """A project transcript whose prompts all count as observable work."""
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for text in prompts:
        records.append({"type": "user", "sessionId": name, "message": {"content": text}})
        records.append({
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Read"}] * tools,
                        "usage": {"output_tokens": output}},
        })
    path = directory / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def corpus(tmp_path):
    """Two projects: one shares vocabulary with the other, one has words of its own."""
    root = tmp_path / "projects"
    transcript(root / "project-alpha", "a1", ["migrate the schema", "naplan results per school"])
    transcript(root / "project-beta", "b1", ["migrate the database", "unrelated wording here"])
    return root


class TestReadArtifact:
    def test_reads_a_well_formed_file(self, tmp_path):
        data, error = report.read_artifact(write_artifact(tmp_path, {"migrate": 1.0}))
        assert error == "" and data["scoring"]["terms"] == {"migrate": 1.0}

    def test_says_how_to_create_a_missing_one(self, tmp_path):
        data, error = report.read_artifact(tmp_path / "absent.json")
        assert data is None and "learn --apply" in error

    def test_reports_corrupt_json(self, tmp_path):
        path = tmp_path / "classifier.json"
        path.write_text("{not json", encoding="utf-8")
        data, error = report.read_artifact(path)
        assert data is None and "cannot read" in error

    def test_reports_an_unreadable_file(self, tmp_path):
        data, error = report.read_artifact(tmp_path)  # a directory reads as an OSError
        assert data is None and "cannot read" in error

    @pytest.mark.parametrize("body", ['"a string"', "[1, 2]", "42"])
    def test_rejects_anything_that_is_not_an_object(self, tmp_path, body):
        path = tmp_path / "classifier.json"
        path.write_text(body, encoding="utf-8")
        data, error = report.read_artifact(path)
        assert data is None and "not a JSON object" in error

    def test_rejects_a_schema_version_the_router_would_ignore(self, tmp_path):
        path = write_artifact(tmp_path, {"migrate": 1.0}, schema_version=99)
        data, error = report.read_artifact(path)
        assert data is None and "the router ignores it" in error

    def test_truncates_a_hostile_schema_version_in_the_message(self, tmp_path):
        path = write_artifact(tmp_path, {"migrate": 1.0}, schema_version="x" * 500)
        _, error = report.read_artifact(path)
        assert "x" * 33 not in error

    def test_refuses_a_file_larger_than_the_router_reads(self, tmp_path):
        path = tmp_path / "classifier.json"
        path.write_text(" " * (router.CLASSIFIER_MAX_BYTES + 1), encoding="utf-8")
        data, error = report.read_artifact(path)
        assert data is None and "larger than" in error


class TestUsableWeights:
    def test_keeps_valid_terms(self):
        weights, invalid, over = report.usable_weights(artifact({"migrate": 1.0, "test": -0.5}))
        assert weights == {"migrate": 1.0, "test": -0.5} and (invalid, over) == (0, 0)

    @pytest.mark.parametrize("terms", [
        {"migrate": "1.0"}, {"migrate": None}, {"migrate": True}, {"migrate": [1]},
        {"ab": 1.0}, {"9lives": 1.0}, {"WITHCAPS": 1.0}, {"a" * 40: 1.0},
    ])
    def test_rejects_what_the_router_rejects(self, terms):
        weights, invalid, _ = report.usable_weights(artifact(terms))
        assert weights == {} and invalid == 1

    def test_counts_terms_past_the_routers_limit(self):
        terms = {f"term{n:04d}": 0.5 for n in range(router.CLASSIFIER_MAX_TERMS + 25)}
        weights, invalid, over = report.usable_weights(artifact(terms))
        assert len(weights) == router.CLASSIFIER_MAX_TERMS and over == 25 and invalid == 0

    @pytest.mark.parametrize("scoring", [None, "terms", {"terms": []}, {}])
    def test_survives_a_missing_or_wrong_shaped_term_table(self, scoring):
        assert report.usable_weights({"scoring": scoring}) == ({}, 0, 0)

    def test_agrees_with_the_router_on_the_same_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MODEL_SWITCHER_HOME", str(tmp_path))
        write_artifact(tmp_path, {"migrate": 1.0, "no": 0.5, "ensure": "x"})
        weights, _, _ = report.usable_weights(artifact({"migrate": 1.0, "no": 0.5, "ensure": "x"}))
        assert weights == router.load_classifier()["terms"]


class TestMaxAdjustment:
    def test_reads_the_artifacts_own_limit(self):
        assert report.max_adjustment_of(artifact({}, scoring={"terms": {}, "max_adjustment": 1.5})) == 1.5

    @pytest.mark.parametrize("limit", [0, -1, 99, True, "3.0", None])
    def test_falls_back_to_the_routers_ceiling_for_anything_unusable(self, limit):
        data = artifact({})
        data["scoring"]["max_adjustment"] = limit
        assert report.max_adjustment_of(data) == router.CLASSIFIER_MAX_ADJUSTMENT


class TestDistribution:
    def test_splits_weights_into_bands(self):
        rows = report.distribution({"a": 0.35, "b": -0.4, "c": 0.6, "d": 1.2, "e": -1.45})
        assert [count for _, _, count in rows] == [2, 1, 1, 1]

    def test_an_empty_band_is_reported_as_zero(self):
        assert [count for _, _, count in report.distribution({"a": 0.4})] == [1, 0, 0, 0]


class TestTieClusters:
    def test_finds_terms_sharing_one_exact_weight(self):
        weights = {"a": -0.394, "b": -0.394, "c": -0.394, "d": 0.9, "e": 0.9}
        (weight, terms), = report.tie_clusters(weights)
        assert weight == -0.394 and terms == ["a", "b", "c"]

    def test_orders_the_biggest_pile_first(self):
        weights = {f"a{n}": -0.394 for n in range(5)} | {f"b{n}": -0.5 for n in range(3)}
        assert [len(terms) for _, terms in report.tie_clusters(weights)] == [5, 3]

    def test_no_cluster_when_every_weight_is_distinct(self):
        assert report.tie_clusters({"a": 0.5, "b": 0.6, "c": 0.7}) == []


class TestAttribute:
    def test_counts_the_projects_a_terms_prompts_came_from(self, corpus):
        found = report.attribute({"migrate", "naplan"}, corpus)
        assert set(found["prompts_of"]["migrate"]) == {"project-alpha", "project-beta"}
        assert set(found["prompts_of"]["naplan"]) == {"project-alpha"}
        assert found["projects"] == 2 and found["files"] == 2 and found["prompts"] == 4

    def test_flags_a_term_confined_to_one_project(self, corpus):
        found = report.attribute({"migrate", "naplan"}, corpus)
        assert report.single_project_terms(found) == ["naplan"]

    def test_reports_per_project_totals(self, corpus):
        found = report.attribute({"migrate", "naplan"}, corpus)
        assert found["by_project"]["project-alpha"] == {"terms": 2, "only": 1}
        assert found["by_project"]["project-beta"] == {"terms": 1, "only": 0}

    def test_a_term_nobody_typed_is_attributed_nowhere(self, corpus):
        found = report.attribute({"kubernetes"}, corpus)
        assert found["prompts_of"]["kubernetes"] == {}

    def test_ignores_prompts_learn_would_not_have_trained_on(self, tmp_path):
        root = tmp_path / "projects"
        transcript(root / "project-alpha", "meta", ["/compact naplan"], tools=14)
        transcript(root / "project-alpha", "chat", ["yes go ahead naplan"], tools=14)
        transcript(root / "project-alpha", "quiet", ["naplan results"], tools=0, output=10)
        found = report.attribute({"naplan"}, root)
        assert found["prompts_of"]["naplan"] == {}, "meta, continuation and unobservable turns"

    def test_reports_a_missing_transcripts_directory(self, tmp_path):
        found = report.attribute({"migrate"}, tmp_path / "absent")
        assert found["missing"] and found["files"] == 0

    def test_ignores_files_outside_a_project_directory(self, tmp_path):
        root = tmp_path / "projects"
        transcript(root, "loose", ["migrate the schema"])
        assert report.attribute({"migrate"}, root)["files"] == 0

    def test_samples_the_newest_transcripts_per_project(self, tmp_path):
        root = tmp_path / "projects"
        old = transcript(root / "project-alpha", "old", ["naplan results"])
        transcript(root / "project-alpha", "new", ["migrate the schema"])
        import os
        os.utime(old, (1, 1))
        found = report.attribute({"migrate", "naplan"}, root, per_project_limit=1)
        assert found["files"] == 1 and found["prompts_of"]["naplan"] == {}
        assert found["sampled"] is True

    def test_stops_tracking_a_term_once_it_reaches_the_project_limit(self, tmp_path):
        root = tmp_path / "projects"
        for name in ("alpha", "beta", "gamma"):
            transcript(root / f"project-{name}", name, ["migrate the schema"])
        found = report.attribute({"migrate"}, root, stop_at=2)
        assert len(found["prompts_of"]["migrate"]) == 2 and found["projects"] == 2

    def test_survives_an_unreadable_transcripts_directory(self, tmp_path, monkeypatch):
        root = tmp_path / "projects"
        transcript(root / "project-alpha", "a", ["migrate the schema"])

        def denied(*args, **kwargs):
            raise PermissionError("nope")

        monkeypatch.setattr(report.Path, "iterdir", denied)
        found = report.attribute({"migrate"}, root)
        assert found["missing"] and found["prompts_of"]["migrate"] == {}

    def test_survives_an_unreadable_transcript(self, tmp_path):
        root = tmp_path / "projects"
        directory = root / "project-alpha"
        directory.mkdir(parents=True)
        (directory / "broken.jsonl").write_text("not json\n{}\n", encoding="utf-8")
        assert report.attribute({"migrate"}, root)["prompts"] == 0


class TestConcentratedTerms:
    def test_flags_a_term_that_is_nearly_all_from_one_project(self, tmp_path):
        root = tmp_path / "projects"
        transcript(root / "project-alpha", "a", ["quiz scores"] * 19)
        transcript(root / "project-beta", "b", ["quiz scores"])
        (term, share), = report.concentrated_terms(report.attribute({"quiz"}, root))
        assert term == "quiz" and share == pytest.approx(0.95)

    def test_ignores_an_evenly_spread_term(self, tmp_path):
        root = tmp_path / "projects"
        transcript(root / "project-alpha", "a", ["migrate the schema"])
        transcript(root / "project-beta", "b", ["migrate the schema"])
        assert report.concentrated_terms(report.attribute({"migrate"}, root)) == []

    def test_a_single_project_term_is_not_double_reported(self, corpus):
        assert report.concentrated_terms(report.attribute({"naplan"}, corpus)) == []


class TestRender:
    def _lines(self, capsys):
        return capsys.readouterr()

    def test_reports_provenance_size_and_attribution(self, tmp_path, corpus, capsys):
        path = write_artifact(tmp_path, {"migrate": 1.1, "naplan": 0.38, "test": -0.2})
        assert report.render(path, corpus) == 0
        out = capsys.readouterr().out
        assert "2,143 prompts from 105 sessions" in out
        assert "544 became real work, 1,599 did not (25% heavy)" in out
        assert "3 in effect, weights -0.20 to +1.10" in out
        assert "weight distribution" in out and "strongest evidence a prompt becomes real work" in out
        assert "project-alpha" in out and "project-beta" in out
        assert "one project only" in out and "naplan +0.38" in out

    def test_notes_the_evidence_floor_pileup(self, tmp_path, corpus, capsys):
        path = write_artifact(tmp_path, {t: -0.394 for t in ("aaa", "bbb", "ccc", "ddd")})
        report.render(path, corpus)
        out = capsys.readouterr().out
        assert "evidence floor  4 terms share exactly -0.394" in out
        assert "indistinguishable" in out

    def test_lists_the_other_tied_weights_too(self, tmp_path, corpus, capsys):
        weights = {f"aa{n}": -0.394 for n in range(4)} | {f"bb{n}": -0.444 for n in range(3)}
        report.render(write_artifact(tmp_path, weights), corpus)
        assert "also 3 at -0.444" in capsys.readouterr().out

    def test_flags_a_term_that_is_nearly_all_from_one_project(self, tmp_path, capsys):
        root = tmp_path / "projects"
        transcript(root / "project-alpha", "a", ["quiz scores"] * 19)
        transcript(root / "project-beta", "b", ["quiz scores"])
        report.render(write_artifact(tmp_path, {"quiz": 0.35, "scores": -0.4}), root)
        out = capsys.readouterr().out
        assert "90%+ from one project" in out and "quiz +0.35" in out

    def test_counts_the_weak_terms(self, tmp_path, corpus, capsys):
        weights = {f"aa{n:03d}": 0.4 for n in range(8)} | {"migrate": 1.1}
        report.render(write_artifact(tmp_path, weights), corpus)
        out = capsys.readouterr().out
        assert "|w| < 0.5" in out and "89%" in out

    def test_says_how_to_create_a_missing_classifier(self, tmp_path, corpus, capsys):
        assert report.render(tmp_path / "absent.json", corpus) == 2
        assert "learn --apply" in capsys.readouterr().err

    def test_reports_a_corrupt_artifact_on_stderr(self, tmp_path, corpus, capsys):
        path = tmp_path / "classifier.json"
        path.write_text("{not json", encoding="utf-8")
        assert report.render(path, corpus) == 2
        captured = capsys.readouterr()
        assert "cannot read" in captured.err and captured.out == ""

    def test_a_classifier_with_no_terms_still_reports_its_provenance(self, tmp_path, corpus, capsys):
        assert report.render(write_artifact(tmp_path, {}), corpus) == 2
        out = capsys.readouterr().out
        assert "2,143 prompts" in out and "none in effect" in out
        assert "nothing here is in effect" in out

    def test_reports_entries_the_router_would_drop(self, tmp_path, corpus, capsys):
        path = write_artifact(tmp_path, {"migrate": 1.0, "ensure": "not a number", "x": 1.0})
        report.render(path, corpus)
        assert "2 entries the router rejects" in capsys.readouterr().out

    def test_reports_a_term_list_far_larger_than_expected(self, tmp_path, corpus, capsys):
        terms = {f"term{n:04d}": 0.6 for n in range(router.CLASSIFIER_MAX_TERMS + 300)}
        assert report.render(write_artifact(tmp_path, terms), corpus) == 0
        out = capsys.readouterr().out
        assert "300 entries past the router's 1,000-term limit" in out

    def test_skips_attribution_without_transcripts(self, tmp_path, capsys):
        path = write_artifact(tmp_path, {"migrate": 1.1})
        assert report.render(path, tmp_path / "absent") == 0
        assert "no transcripts directory" in capsys.readouterr().out

    def test_says_when_no_term_is_tied_to_one_project(self, tmp_path, corpus, capsys):
        report.render(write_artifact(tmp_path, {"migrate": 1.1}), corpus)
        assert "no term is tied to a single project" in capsys.readouterr().out

    def test_survives_metadata_of_the_wrong_type(self, tmp_path, corpus, capsys):
        path = write_artifact(
            tmp_path, {"migrate": 1.1},
            corpus={"sessions": "many", "prompts": None, "heavy": [], "light": 3},
            generated_at={"when": "never"}, generator=["odd"],
        )
        assert report.render(path, corpus) == 0
        out = capsys.readouterr().out
        assert "? prompts from ? sessions" in out and "heavy/light split not recorded" in out
        assert "unknown" in out

    def test_truncates_a_very_long_generator_string(self, tmp_path, corpus, capsys):
        path = write_artifact(tmp_path, {"migrate": 1.1}, generator="g" * 500)
        report.render(path, corpus)
        assert "g" * 500 not in capsys.readouterr().out

    def test_shortens_a_long_project_name(self):
        assert report._label("x" * 80).startswith("...")
        assert len(report._label("x" * 80)) == report.PROJECT_LABEL_CHARS

    def test_lists_only_the_top_projects(self, tmp_path, capsys):
        root = tmp_path / "projects"
        for n in range(report.TOP_PROJECTS + 3):
            transcript(root / f"project-{n:02d}", f"s{n}", ["migrate the schema"])
        report.render(write_artifact(tmp_path, {"migrate": 1.1}), root)
        assert "and 3 more projects" in capsys.readouterr().out

    def test_caps_how_many_topical_terms_it_lists(self, tmp_path, capsys):
        root = tmp_path / "projects"
        terms = [f"topic{n:03d}" for n in range(report.TOP_SINGLE_PROJECT + 5)]
        transcript(root / "project-alpha", "a", [" ".join(terms)])
        transcript(root / "project-beta", "b", ["migrate the schema"])
        weights = {term: 0.6 for term in terms} | {"migrate": 1.1}
        report.render(write_artifact(tmp_path, weights), root)
        # The 35 topic terms and 'migrate' are each confined to one project: 36, listed 30.
        assert "and 6 more" in capsys.readouterr().out

    def test_reports_terms_no_transcript_contains(self, tmp_path, corpus, capsys):
        report.render(write_artifact(tmp_path, {"kubernetes": 1.1, "migrate": 0.9}), corpus)
        assert "1 terms matched no prompt" in capsys.readouterr().out
