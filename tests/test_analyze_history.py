import json
import stat

import pytest

import analyze_history as learn


def assistant(tools=(), output=0):
    content = [{"type": "tool_use", "name": name} for name in tools]
    return {"type": "assistant", "message": {"content": content, "usage": {"output_tokens": output}}}


def user(text, session="s1", meta=False, sidechain=False):
    return {
        "type": "user", "sessionId": session, "isMeta": meta, "isSidechain": sidechain,
        "message": {"content": text},
    }


def write_transcript(directory, name, records):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def turn(text, session="s1", tools=0, mutations=0, delegations=0, output=500):
    return {
        "text": text, "session": session, "tools": tools, "mutations": mutations,
        "delegations": delegations, "output": output,
    }


def corpus(heavy_phrase, light_phrase, sessions=6, per_session=30):
    """A corpus where one phrase reliably precedes work and another reliably does not."""
    turns = []
    for s in range(sessions):
        for i in range(per_session):
            if i % 2:
                turns.append(turn(f"{heavy_phrase} the module", session=f"s{s}", tools=14))
            else:
                turns.append(turn(f"{light_phrase} the value", session=f"s{s}", tools=1))
    return learn.trainable_turns(turns)


class TestPromptText:
    def test_reads_a_plain_string(self):
        assert learn.prompt_text({"content": "hello"}) == "hello"

    def test_joins_text_blocks(self):
        message = {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
        assert learn.prompt_text(message) == "a\nb"

    def test_ignores_tool_result_blocks(self):
        assert learn.prompt_text({"content": [{"type": "tool_result", "content": "x"}]}) == ""

    @pytest.mark.parametrize("message", [None, "string", 5, {"content": 5}, {}])
    def test_survives_anything_else(self, message):
        assert learn.prompt_text(message) == ""


class TestIterTurns:
    def test_attributes_following_tool_calls_to_the_prompt(self, tmp_path):
        path = write_transcript(tmp_path, "t", [
            user("refactor the auth module"),
            assistant(tools=["Read", "Edit", "Edit"], output=400),
            assistant(tools=["Agent"], output=100),
        ])
        (result,) = list(learn.iter_turns(path))
        assert result["tools"] == 4 and result["mutations"] == 2
        assert result["delegations"] == 1 and result["output"] == 500

    def test_splits_on_each_new_prompt(self, tmp_path):
        path = write_transcript(tmp_path, "t", [
            user("first"), assistant(tools=["Read"]),
            user("second"), assistant(tools=["Edit", "Edit"]),
        ])
        first, second = list(learn.iter_turns(path))
        assert first["tools"] == 1 and second["mutations"] == 2

    def test_skips_meta_and_sidechain_prompts(self, tmp_path):
        path = write_transcript(tmp_path, "t", [
            user("meta prompt", meta=True), user("sidechain prompt", sidechain=True),
            user("real prompt"), assistant(tools=["Read"]),
        ])
        assert [t["text"] for t in learn.iter_turns(path)] == ["real prompt"]

    def test_skips_tool_result_turns(self, tmp_path):
        path = write_transcript(tmp_path, "t", [
            user("real prompt"),
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            assistant(tools=["Read"]),
        ])
        assert len(list(learn.iter_turns(path))) == 1

    def test_ignores_assistant_output_before_any_prompt(self, tmp_path):
        path = write_transcript(tmp_path, "t", [assistant(tools=["Read"]), user("p"), assistant(tools=["Edit"])])
        (result,) = list(learn.iter_turns(path))
        assert result["tools"] == 1

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "t.jsonl"
        tmp_path.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json\n[1,2]\n" + json.dumps(user("p")) + "\n", encoding="utf-8")
        assert [t["text"] for t in learn.iter_turns(path)] == ["p"]

    def test_survives_a_malformed_assistant_record(self, tmp_path):
        path = write_transcript(tmp_path, "t", [
            user("p"), {"type": "assistant", "message": "nope"},
            {"type": "assistant", "message": {"content": "nope", "usage": "nope"}},
        ])
        (result,) = list(learn.iter_turns(path))
        assert result["tools"] == 0 and result["output"] == 0

    def test_returns_nothing_for_an_unreadable_file(self, tmp_path):
        assert list(learn.iter_turns(tmp_path / "absent.jsonl")) == []

    def test_keeps_the_raw_usage_of_each_turn_for_re_pricing(self, tmp_path):
        path = write_transcript(tmp_path, "t", [
            user("refactor it"),
            {"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 10, "output_tokens": 20}}},
            {"type": "assistant", "message": {"id": "m2", "usage": {"input_tokens": 5, "output_tokens": 1}}},
        ])
        (result,) = list(learn.iter_turns(path))
        assert [u["input_tokens"] for u in result["usage"].values()] == [10, 5]

    def test_counts_a_streamed_message_once(self, tmp_path):
        """Streaming rewrites the same message id; only the last entry carries final usage."""
        path = write_transcript(tmp_path, "t", [
            user("refactor it"),
            {"type": "assistant", "message": {"id": "m1", "usage": {"output_tokens": 5}}},
            {"type": "assistant", "message": {"id": "m1", "usage": {"output_tokens": 40}}},
        ])
        (result,) = list(learn.iter_turns(path))
        assert [u["output_tokens"] for u in result["usage"].values()] == [40]

    def test_keeps_entries_that_carry_no_identifier_apart(self, tmp_path):
        path = write_transcript(tmp_path, "t", [user("p"), assistant(output=7), assistant(output=9)])
        (result,) = list(learn.iter_turns(path))
        assert sorted(u["output_tokens"] for u in result["usage"].values()) == [7, 9]


class TestLabelling:
    @pytest.mark.parametrize("text", ["yes", "go ahead", "ok", "proceed", "yes please continue"])
    def test_continuations_are_excluded(self, text):
        assert learn.is_continuation(text)

    @pytest.mark.parametrize("text", ["refactor the auth module", "yes but first rewrite the parser and " * 5])
    def test_substantive_prompts_are_not_continuations(self, text):
        assert not learn.is_continuation(text)

    @pytest.mark.parametrize("text", ["/compact", "  /clear", "<command-name>foo</command-name>"])
    def test_meta_prompts_are_excluded(self, text):
        assert learn.is_meta_prompt(text)

    def test_a_turn_with_no_observable_work_is_dropped(self):
        assert not learn.is_observable(turn("p", tools=0, output=10))

    def test_a_delegating_turn_is_heavy(self):
        assert learn.is_heavy(turn("p", delegations=1))

    def test_a_single_edit_is_not_heavy(self):
        assert not learn.is_heavy(turn("p", tools=2, mutations=1))

    def test_many_edits_are_heavy(self):
        assert learn.is_heavy(turn("p", tools=5, mutations=3))

    def test_output_tokens_alone_never_make_a_turn_heavy(self):
        # Thinking models inflate output; verbosity is not work.
        assert not learn.is_heavy(turn("p", tools=0, output=100_000))

    def test_trainable_turns_filters_and_labels(self):
        kept = learn.trainable_turns([
            turn("yes"), turn("/compact"), turn("p", tools=0, output=1),
            turn("refactor everything", tools=14),
        ])
        assert [t["text"] for t in kept] == ["refactor everything"]
        assert kept[0]["heavy"] is True


class TestTermExtraction:
    def test_keeps_ordinary_words(self):
        assert learn.terms_in("Refactor the Auth Module") == {"refactor", "auth", "module"}

    def test_deduplicates(self):
        assert learn.terms_in("migrate migrate migrate") == {"migrate"}

    @pytest.mark.parametrize("text,absent", [
        ("the and but for with", "the"),
        ("sk-ant-api03-abcdef123456", "sk-ant-api03-abcdef123456"),
        ("jiggys-macbook-pro", "jiggys-macbook-pro"),
        ("a bc", "bc"),
        ("feature/JIRA-1234-fix", "jira-1234-fix"),
    ])
    def test_excludes_unsafe_or_low_signal_tokens(self, text, absent):
        assert absent not in learn.terms_in(text)

    def test_keeps_a_single_hyphen_term(self):
        assert "real-time" in learn.terms_in("the real-time pipeline")

    def test_rejects_long_tokens_containing_digits(self):
        assert not learn.is_usable_term("abc123def456ghi")

    def test_keeps_short_tokens_containing_digits(self):
        assert learn.is_usable_term("oauth2") and learn.is_usable_term("k8s")

    def test_ignores_text_beyond_the_scoring_limit(self):
        text = "x" * 10_000 + " needleterm"
        assert "needleterm" not in learn.terms_in(text)


class TestLearnWeights:
    def test_separates_predictive_terms_by_sign(self):
        weights = learn.learn_weights(corpus("refactor", "rename"))
        assert weights["refactor"] > 0 > weights["rename"]

    def test_requires_support_across_distinct_sessions(self):
        # The same term, many times, but only ever in one session.
        turns = learn.trainable_turns(
            [turn("singleton term here", session="only", tools=14) for _ in range(40)]
            + [turn("other words entirely", session=f"s{i}", tools=1) for i in range(40)]
        )
        assert "singleton" not in learn.learn_weights(turns)

    def test_requires_enough_occurrences(self):
        turns = learn.trainable_turns(
            [turn("rareword appears", session=f"s{i}", tools=14) for i in range(3)]
            + [turn("common filler words", session=f"s{i}", tools=1) for i in range(60)]
        )
        assert "rareword" not in learn.learn_weights(turns)

    def test_thin_evidence_weighs_less_than_thick_evidence(self):
        """Shrinkage: same perfect ratio, fewer observations, smaller weight."""
        thin = [turn("scarce word", session=f"s{i}", tools=14) for i in range(learn.MIN_OCCURRENCES)]
        thick = [turn("abundant word", session=f"s{i}", tools=14) for i in range(learn.MIN_OCCURRENCES * 20)]
        light = [turn("plain filler text", session=f"s{i}", tools=1) for i in range(200)]
        weights = learn.learn_weights(learn.trainable_turns(thin + thick + light))
        assert weights["abundant"] > weights["scarce"] > 0

    def test_every_weight_is_within_bounds(self):
        weights = learn.learn_weights(corpus("refactor", "rename"))
        assert all(-learn.MAX_TERM_WEIGHT <= w <= learn.MAX_TERM_WEIGHT for w in weights.values())
        assert all(abs(w) >= learn.MIN_TERM_WEIGHT for w in weights.values())

    def test_caps_the_number_of_terms(self, monkeypatch):
        monkeypatch.setattr(learn, "MAX_TERMS", 2)
        assert len(learn.learn_weights(corpus("refactor", "rename"))) <= 2

    def test_returns_nothing_without_both_classes(self):
        only_heavy = learn.trainable_turns([turn(f"refactor module {i}", tools=14) for i in range(40)])
        assert learn.learn_weights(only_heavy) == {}

    def test_is_deterministic(self):
        turns = corpus("refactor", "rename")
        assert learn.learn_weights(turns) == learn.learn_weights(turns)


class TestAdjustment:
    def test_sums_matching_terms(self):
        assert learn.learned_adjustment("refactor now", {"refactor": 0.5}) == 0.5

    def test_counts_a_repeated_term_once(self):
        assert learn.learned_adjustment("refactor refactor refactor", {"refactor": 0.5}) == 0.5

    def test_is_clamped_in_both_directions(self):
        many = {f"term{i}": 1.5 for i in range(20)}
        text = " ".join(many)
        assert learn.learned_adjustment(text, many) == learn.MAX_ADJUSTMENT
        assert learn.learned_adjustment(text, {k: -1.5 for k in many}) == -learn.MAX_ADJUSTMENT

    def test_unknown_terms_contribute_nothing(self):
        assert learn.learned_adjustment("nothing familiar here", {"refactor": 1.0}) == 0.0


class TestReporting:
    def test_classifier_carries_corpus_counts_but_no_prompt_text(self):
        turns = corpus("refactor", "rename")
        built = learn.build_classifier(turns, learn.learn_weights(turns), "2026-07-25T00:00:00Z")
        assert built["corpus"]["prompts"] == len(turns)
        assert built["corpus"]["heavy"] + built["corpus"]["light"] == len(turns)
        assert "the module" not in json.dumps(built), "no prompt text may reach the artifact"

    def test_report_shows_both_baselines(self):
        turns = corpus("refactor", "rename")
        built = learn.build_classifier(turns, learn.learn_weights(turns), "")
        text = learn.report(built, turns, 5)
        assert "built-in" in text and "with terms" in text and "precision" in text

    def test_accuracy_handles_a_corpus_with_no_heavy_turns(self):
        turns = learn.trainable_turns([turn("rename it", tools=1) for _ in range(10)])
        assert learn._accuracy(turns, 5, None)["precision"] == 0.0


class TestCollect:
    def test_reads_nested_and_flat_layouts(self, tmp_path):
        write_transcript(tmp_path / "projects" / "proj", "a", [user("nested"), assistant(tools=["Read"])])
        write_transcript(tmp_path / "projects", "b", [user("flat"), assistant(tools=["Read"])])
        found = {t["text"] for t in learn.collect([tmp_path / "projects"], None)}
        assert found == {"nested", "flat"}

    def test_ignores_a_missing_directory(self, tmp_path):
        assert learn.collect([tmp_path / "absent"], None) == []

    def test_honours_the_session_cap(self, tmp_path):
        for i in range(5):
            write_transcript(tmp_path / "p", f"t{i}", [user(f"p{i}"), assistant(tools=["Read"])])
        assert len(learn.collect([tmp_path / "p"], 2)) == 2


class TestMain:
    def _corpus_dir(self, tmp_path, sessions=8, per_session=30):
        directory = tmp_path / "projects" / "proj"
        for s in range(sessions):
            records = []
            for i in range(per_session):
                if i % 2:
                    records += [user("refactor the auth module", session=f"s{s}"), assistant(tools=["Edit"] * 14)]
                else:
                    records += [user("rename the local value", session=f"s{s}"), assistant(tools=["Read"])]
            write_transcript(directory, f"t{s}", records)
        return directory

    def test_writes_a_candidate_without_changing_routing(self, tmp_path, capsys):
        home = tmp_path / "home"
        home.mkdir()
        code = learn.main(["--home", str(home), "--transcripts", str(self._corpus_dir(tmp_path))])
        assert code == 0
        assert (home / "classifier.candidate.json").exists()
        assert not (home / "classifier.json").exists(), "routing must not change without --apply"
        assert "re-run with --apply" in capsys.readouterr().out.lower()

    def test_apply_promotes_the_candidate(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        learn.main(["--home", str(home), "--transcripts", str(self._corpus_dir(tmp_path)), "--apply"])
        live = json.loads((home / "classifier.json").read_text(encoding="utf-8"))
        assert live["schema_version"] == learn.SCHEMA_VERSION
        assert live["scoring"]["terms"]["refactor"] > 0

    def test_the_artifact_is_owner_readable_only(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        learn.main(["--home", str(home), "--transcripts", str(self._corpus_dir(tmp_path)), "--apply"])
        mode = stat.S_IMODE((home / "classifier.json").stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_refuses_when_there_is_too_little_history(self, tmp_path, capsys):
        home = tmp_path / "home"
        home.mkdir()
        directory = tmp_path / "projects" / "proj"
        write_transcript(directory, "t", [user("refactor it"), assistant(tools=["Edit"] * 14)])
        code = learn.main(["--home", str(home), "--transcripts", str(directory)])
        assert code == 1 and "usable prompts" in capsys.readouterr().err
        assert not (home / "classifier.candidate.json").exists()

    def test_refuses_when_no_term_earns_a_weight(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(learn, "MIN_TERM_WEIGHT", 99.0)
        home = tmp_path / "home"
        home.mkdir()
        code = learn.main(["--home", str(home), "--transcripts", str(self._corpus_dir(tmp_path))])
        assert code == 1 and "evidence threshold" in capsys.readouterr().err

    def test_announces_what_it_reads_before_reading_it(self, tmp_path, capsys):
        home = tmp_path / "home"
        home.mkdir()
        directory = self._corpus_dir(tmp_path)
        learn.main(["--home", str(home), "--transcripts", str(directory)])
        out = capsys.readouterr().out
        assert str(directory) in out and "nothing leaves this machine" in out
