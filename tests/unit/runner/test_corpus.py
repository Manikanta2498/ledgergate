"""The corpus, runner, result document, renderers and drift against docs/spec/corpus.md."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from ledgergate.cli.__main__ import main
from ledgergate.report import (
    ResultError,
    drift,
    dump_result,
    json_schema,
    load_result,
    render_junit,
    render_markdown,
    render_sarif,
)
from ledgergate.runner import CorpusError, load_corpus, run
from ledgergate.trace import dump_v2, load_any

CORPUS = Path("corpus")


_COPIES = 0


def _copy_corpus(tmp_path: Path) -> Path:
    global _COPIES
    _COPIES += 1
    dst = tmp_path / f"corpus{_COPIES}"
    shutil.copytree(CORPUS / "scenarios", dst / "scenarios")
    shutil.copytree(CORPUS / "expectations", dst / "expectations")
    return dst


class TestShippedCorpus:
    def test_every_scenario_passes_from_its_script_and_the_result_is_deterministic(self) -> None:
        corpus = load_corpus(CORPUS)
        assert len([s for s in corpus.scenarios if s.kind == "correct"]) >= 8
        assert len([s for s in corpus.scenarios if s.kind == "red-team"]) >= 8
        first = run(corpus)
        second = run(corpus)
        assert dump_result(first) == dump_result(second)
        assert first.gate == 0
        for sc in first.scenarios:
            assert sc.status == "pass" and sc.source == "script", (sc.id, sc.error, sc.expectations)
        assert (
            first.summary.by_kind["red-team"].pass_ == first.summary.by_kind["red-team"].scenarios
        )

    def test_red_team_expectations_name_the_containing_mechanism(self) -> None:
        corpus = load_corpus(CORPUS)
        for sc in corpus.scenarios:
            if sc.kind != "red-team":
                continue
            ex = corpus.expectations[sc.id]
            named = (
                set(ex.dispositions or {}) | set(ex.outcomes or {}) | set(ex.matched_rules or {})
            )
            assert named & {
                "invalid",
                "denied",
                "rejected",
                "conflict",
                "awaiting_approval",
                "runtime.approval_rejected",
            }, sc.id

    def test_traces_differ_between_runs_but_behavioural_digests_do_not(
        self, tmp_path: Path
    ) -> None:
        corpus = load_corpus(CORPUS)
        a, b = tmp_path / "a", tmp_path / "b"
        ra = run(corpus, only=("post-and-reverse",), keep_traces=a)
        rb = run(corpus, only=("post-and-reverse",), keep_traces=b)
        assert (a / "post-and-reverse.json").read_text() != (
            b / "post-and-reverse.json"
        ).read_text()
        assert ra.scenarios[0].trace_digest == rb.scenarios[0].trace_digest

    def test_result_schema_artefact_matches_the_model(self) -> None:
        checked_in = json.loads(Path("schema/result/v1.json").read_text())
        assert checked_in == json_schema()
        r = run(load_corpus(CORPUS), only=("read-balance",))
        import jsonschema

        jsonschema.validate(json.loads(dump_result(r)), checked_in)
        assert load_result(dump_result(r)) == r


class TestLivePath:
    @pytest.mark.parametrize(
        "scenario_id", ["retry-replays", "post-and-reverse", "reverse-setup-entry"]
    )
    def test_emit_setup_then_serve_then_score_gives_the_same_digest(
        self, tmp_path: Path, scenario_id: str
    ) -> None:
        """The live path's evidence: the scenario's own script replayed through serve under a
        system clock and random ids, entry_ref resolved against that journal, scores pass with
        the scripted digest; post-and-reverse covers reverse by agent position, and
        reverse-setup-entry a reverse of a setup entry that is then *replayed* (no ledger pair)."""
        corpus = load_corpus(CORPUS)
        sc = next(s for s in corpus.scenarios if s.id == scenario_id)
        journal = tmp_path / "live.journal"
        assert main(["run", "--corpus", str(CORPUS), "--emit-setup", sc.id, str(journal)]) == 0
        policy = journal.with_name(journal.name + ".policy.json")
        assert policy.exists()
        entries: dict[str, str] = {}
        for call, result in _pairs(load_any(_derive(journal))):
            if result is not None:
                entries[call] = result
        args_cmd = [
            sys.executable,
            "-m",
            "ledgergate.cli",
            "serve",
            "--journal",
            str(journal),
            "--policy",
            str(policy),
        ]
        for n, step in enumerate(sc.agent.script or (), start=1):
            args = dict(step.arguments or {})
            if "entry_ref" in args:
                args["entry_id"] = entries[args.pop("entry_ref")]
            if step.key is not None:
                args["idempotency_key"] = step.key
            line = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": n,
                    "method": "tools/call",
                    "params": {"name": step.tool, "arguments": args},
                }
            )
            proc = subprocess.run(
                args_cmd, input=(line + "\n").encode(), capture_output=True, check=False
            )
            assert proc.returncode == 0, proc.stderr
            response = json.loads(proc.stdout)["result"]["structuredContent"]
            result = response.get("result")
            if response.get("ok") and isinstance(result, dict) and "entry_id" in result:
                entries[f"agent-{n}"] = result["entry_id"]
        traces = tmp_path / "traces"
        traces.mkdir()
        (traces / f"{sc.id}.json").write_text(_derive(journal))
        live = run(corpus, only=(sc.id,), traces=traces)
        scripted = run(corpus, only=(sc.id,))
        assert live.scenarios[0].status == "pass", live.scenarios[0]
        assert live.scenarios[0].source == "trace"
        assert live.scenarios[0].trace_digest == scripted.scenarios[0].trace_digest

    def test_a_trace_from_another_setup_is_a_setup_mismatch(self, tmp_path: Path) -> None:
        corpus = load_corpus(CORPUS)
        traces = tmp_path / "traces"
        run(corpus, only=("post-and-reverse",), keep_traces=traces)  # a setup with no `before`
        (traces / "trial-balance.json").write_text((traces / "post-and-reverse.json").read_text())
        r = run(corpus, only=("trial-balance",), traces=traces)  # whose setup settles a transaction
        assert r.scenarios[0].status == "error" and r.scenarios[0].error is not None
        assert r.scenarios[0].error.startswith("setup mismatch")
        # a v1 observational trace lifts to legacy and is never from a setup
        v1 = {
            "schema_version": "1",
            "trace_id": "t",
            "agent": {"name": "a"},
            "started_at": "2026-01-01T00:00:00Z",
            "ended_at": "2026-01-01T00:00:00Z",
            "events": [],
        }
        (traces / "trial-balance.json").write_text(json.dumps(v1))
        r = run(corpus, only=("trial-balance",), traces=traces)
        assert r.scenarios[0].status == "error"
        (traces / "trial-balance.json").write_text("{")
        r = run(corpus, only=("trial-balance",), traces=traces)
        assert r.scenarios[0].error is not None and r.scenarios[0].error.startswith(
            "unreadable trace"
        )

    def test_emit_setup_refuses_scripted_only_and_existing_paths(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        journal = tmp_path / "j"
        assert (
            main(["run", "--corpus", str(CORPUS), "--emit-setup", "refund-over-cap", str(journal)])
            == 2
        )
        assert "scripted_only" in capsys.readouterr().err
        assert (
            main(["run", "--corpus", str(CORPUS), "--emit-setup", "read-balance", str(journal)])
            == 0
        )
        assert (
            main(["run", "--corpus", str(CORPUS), "--emit-setup", "read-balance", str(journal)])
            == 2
        )


class TestCorpusFaults:
    def _fault(self, tmp_path: Path, mutate: Any) -> str:
        root = _copy_corpus(tmp_path)
        mutate(root)
        with pytest.raises(CorpusError) as info:
            load_corpus(root)
        return str(info.value)

    def test_each_fault_is_named(self, tmp_path: Path) -> None:
        def edit(root: Path, rel: str, fn: Any) -> None:
            p = root / rel
            doc = yaml.safe_load(p.read_text())
            fn(doc)
            p.write_text(yaml.safe_dump(doc))

        assert "expectations without scenarios" in self._fault(
            tmp_path,
            lambda r: (
                (r / "expectations" / "read-balance.yaml").unlink()
                or (r / "expectations" / "ghost.yaml").write_text(
                    "schema_version: '1'\nid: ghost\n"
                )
            ),
        )
        assert "unknown kinds" in self._fault(
            tmp_path,
            lambda r: edit(
                r,
                "expectations/read-balance.yaml",
                lambda d: d.__setitem__("dispositions", {"weird": 1}),
            ),
        )
        assert "messages_contain" in self._fault(
            tmp_path,
            lambda r: edit(
                r,
                "expectations/read-balance.yaml",
                lambda d: d.__setitem__("messages_contain", "x"),
            ),
        )
        assert "does not match directory" in self._fault(
            tmp_path,
            lambda r: edit(
                r,
                "scenarios/correct/read-balance.yaml",
                lambda d: d.__setitem__("kind", "red-team"),
            ),
        )
        assert "status" in self._fault(
            tmp_path,
            lambda r: edit(
                r, "expectations/read-balance.yaml", lambda d: d.__setitem__("status", "fail")
            ),
        )
        assert "entry_ref" in self._fault(
            tmp_path,
            lambda r: edit(
                r,
                "scenarios/correct/post-and-reverse.yaml",
                lambda d: d["agent"]["script"][1]["arguments"].__setitem__("entry_ref", "agent-9"),
            ),
        )

        def capped_before(d: dict[str, Any]) -> None:
            d["scripted_only"] = False
            d["setup"]["before"].append(d["agent"]["script"][0] | {"key": "setup-9"})

        assert "scripted_only" in self._fault(
            tmp_path,
            lambda r: edit(r, "scenarios/red-team/refund-over-cap.yaml", capped_before),
        )
        assert "empty corpus" in self._fault(
            tmp_path,
            lambda r: [
                p.unlink()
                for p in list(r.glob("scenarios/*/*.yaml")) + list(r.glob("expectations/*.yaml"))
            ],
        )

    def test_only_unknown_is_a_corpus_fault_and_exit_codes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["run", "--corpus", str(CORPUS), "--only", "nope"]) == 2
        assert "unknown scenarios" in capsys.readouterr().err
        assert main(["run", "--corpus", str(tmp_path / "missing")]) == 2
        out = tmp_path / "r.json"
        assert main(["run", "--corpus", str(CORPUS), "--kind", "correct", "--out", str(out)]) == 0
        assert load_result(out.read_text()).selection.kind == "correct"

    def test_unresolved_entry_ref_at_run_time_is_a_scenario_error(self, tmp_path: Path) -> None:
        root = _copy_corpus(tmp_path)
        p = root / "scenarios" / "correct" / "post-and-reverse.yaml"
        doc = yaml.safe_load(p.read_text())
        doc["agent"]["script"][0]["arguments"]["draft"]["postings"][1]["money"]["amount"] = (
            1  # unbalanced: applies no entry
        )
        p.write_text(yaml.safe_dump(doc))
        r = run(load_corpus(root), only=("post-and-reverse",))
        assert (
            r.scenarios[0].status == "error"
            and r.scenarios[0].error == "unresolved entry_ref: agent-1"
        )


def _derive(journal: Path) -> str:
    from ledgergate.derive import trace as derive_trace

    return dump_v2(derive_trace(str(journal)))


def _pairs(trace: Any) -> list[tuple[str, str | None]]:
    """(call_id, entry_id or None) per invocation of a v2 trace, in order."""
    out: list[tuple[str, str | None]] = []
    call: str | None = None
    entry: str | None = None
    for e in trace.events:
        if e.type == "tool_call":
            call, entry = e.call_id, None
        elif e.type == "ledger_result":
            entry = e.entry_id
        elif e.type == "tool_result" and call is not None:
            out.append((call, entry))
    return out


def _resummarize(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    from ledgergate.report import ScenarioResult, summarize

    return summarize([ScenarioResult.model_validate(s) for s in scenarios]).model_dump(
        mode="json", by_alias=True
    )


class TestReport:
    def test_renderers_and_drift_gate(self, tmp_path: Path) -> None:
        corpus = load_corpus(CORPUS)
        base = run(corpus, only=("read-balance", "retry-replays", "refund-over-cap"))
        md = render_markdown(base)
        assert "| `read-balance` | correct | pass |" in md
        junit = render_junit(base)
        assert 'tests="2" failures="0" errors="0" skipped="0"' in junit
        sarif = json.loads(render_sarif(base))
        assert sarif["version"] == "2.1.0" and sarif["runs"][0]["results"] == []
        rules = {r["id"] for r in sarif["runs"][0]["tool"]["driver"]["rules"]}
        assert {
            "expectation/status",
            "runner/setup-mismatch",
            "books_balance_and_chain_verifies",
        } <= rules
        # a candidate with a regression, a newly skipped and a newly scored failure
        cand_doc = json.loads(dump_result(base))
        for s in cand_doc["scenarios"]:
            if s["id"] == "retry-replays":
                s["status"] = "fail"
                s["expectations"][0]["status"] = "fail"
        cand_doc["summary"] = _resummarize(cand_doc["scenarios"])
        cand = load_result(json.dumps(cand_doc))
        d = drift(base, cand)
        assert {r.id: r.bucket for r in d.rows}["retry-replays"] == "regressed" and d.gate == 1
        assert all(r.same_trace for r in d.rows if r.id != "retry-replays")
        sarif_fail = json.loads(render_sarif(cand))
        assert sarif_fail["runs"][0]["results"][0]["ruleId"].startswith("expectation/")
        junit_fail = render_junit(cand)
        assert "<failure" in junit_fail and 'failures="1"' in junit_fail
        skipped_doc = json.loads(dump_result(base))
        skipped_doc["scenarios"][0].update(
            {
                "status": "skipped",
                "source": "none",
                "trace_digest": None,
                "scorecard": None,
                "expectations": [],
            }
        )
        skipped_doc["summary"] = _resummarize(skipped_doc["scenarios"])
        sk = load_result(json.dumps(skipped_doc))
        assert drift(base, sk).gate == 1 and drift(base, sk, allow_newly_skipped=True).gate == 0
        assert drift(sk, base).gate == 0  # newly scored pass
        assert (
            drift(sk, cand).gate == 1
        )  # newly scored... retry-replays regressed; also the newly scored one passes
        other = load_result(
            json.dumps({**json.loads(dump_result(base)), "corpus_digest": "0" * 64})
        )
        with pytest.raises(ResultError, match="different corpora"):
            drift(base, other)
        assert (
            "skipped" in render_junit(sk)
            and json.loads(render_sarif(sk))["runs"][0]["invocations"][0][
                "toolExecutionNotifications"
            ]
        )

    def test_report_cli_exit_codes(self, tmp_path: Path) -> None:
        out = tmp_path / "r.json"
        assert (
            main(["run", "--corpus", str(CORPUS), "--only", "read-balance", "--out", str(out)]) == 0
        )
        assert (
            main(["report", str(out), "--format", "junit", "--out", str(tmp_path / "j.xml")]) == 0
        )
        assert main(["report", str(out), "--format", "json"]) == 2
        assert main(["report", str(tmp_path / "missing.json")]) == 2
        assert main(["report", "--drift", str(out), str(out)]) == 0
        assert main(["report", "--drift", str(out)]) == 2


class TestImplementationReview:
    def test_chart_faults_and_non_ijson_steps_are_corpus_faults(self, tmp_path: Path) -> None:
        def edit(root: Path, rel: str, fn: Any) -> None:
            p = root / rel
            doc = yaml.safe_load(p.read_text())
            fn(doc)
            p.write_text(yaml.safe_dump(doc))

        for mutate, needle in (
            (lambda d: d["setup"]["chart"][0].__setitem__("currency", "XYZ"), "setup.chart"),
            (lambda d: d["setup"]["chart"].append(dict(d["setup"]["chart"][0])), "setup.chart"),
            (lambda d: d["agent"]["script"][0]["arguments"].__setitem__("n", 2**60), "not I-JSON"),
        ):
            root = _copy_corpus(tmp_path)
            edit(root, "scenarios/correct/read-balance.yaml", mutate)
            with pytest.raises(CorpusError, match=needle):
                load_corpus(root)

    def test_report_fails_closed_on_an_unknown_runner_error(self) -> None:
        r = run(load_corpus(CORPUS), only=("read-balance",))
        doc = json.loads(dump_result(r))
        doc["scenarios"][0].update(
            {
                "status": "error",
                "error": "something else",
                "trace_digest": None,
                "scorecard": None,
                "expectations": [],
            }
        )
        bad = json.dumps({**doc, "summary": json.loads(dump_result(r))["summary"]})
        with pytest.raises(ResultError, match="outside the vocabulary"):
            load_result(bad)  # the model, not a renderer, refuses it
        good = json.loads(dump_result(r))
        good["scenarios"][0]["error"] = "setup mismatch: x"  # error on a pass row
        with pytest.raises(ResultError, match="exactly for an error"):
            load_result(json.dumps(good))
        other = json.loads(dump_result(r))
        other["scenarios"] = []
        other["summary"] = _resummarize([])
        with pytest.raises(ResultError, match="same scenario ids"):
            drift(r, load_result(json.dumps(other)))

    def test_emit_setup_failure_inside_before_leaves_nothing_and_a_retry_succeeds(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _copy_corpus(tmp_path)
        p = root / "scenarios" / "correct" / "reverse-setup-entry.yaml"
        doc = yaml.safe_load(p.read_text())
        # the setup's post is unbalanced: it applies no entry, so a `before` reverse by
        # entry_ref fails inside _apply, after the journal and policy exist
        doc["setup"]["before"][0]["arguments"]["draft"]["postings"][1]["money"]["amount"] = 1
        doc["setup"]["before"].append(
            {"tool": "reverse", "key": "setup-2", "arguments": {"entry_ref": "setup-1"}}
        )
        p.write_text(yaml.safe_dump(doc))
        target = tmp_path / "j"
        args = ["run", "--corpus", str(root), "--emit-setup", "reverse-setup-entry", str(target)]
        assert main(args) == 2
        assert "failed while applying before" in capsys.readouterr().err
        assert not target.exists() and not target.with_name("j.policy.json").exists()
        # the same target is free for a retry against a good scenario
        assert (
            main(["run", "--corpus", str(root), "--emit-setup", "read-balance", str(target)]) == 0
        )

    def test_scripted_only_refuses_a_supplied_trace(self, tmp_path: Path) -> None:
        corpus = load_corpus(CORPUS)
        traces = tmp_path / "t"
        run(corpus, only=("refund-over-cap",), keep_traces=traces)
        r = run(corpus, only=("refund-over-cap",), traces=traces)
        assert r.scenarios[0].status == "error"
        assert r.scenarios[0].error is not None and "scripted_only" in r.scenarios[0].error

    def test_out_to_an_unwritable_path_is_exit_2(self, tmp_path: Path) -> None:
        bad_out = str(tmp_path / "no" / "r.json")
        assert (
            main(["run", "--corpus", str(CORPUS), "--only", "read-balance", "--out", bad_out]) == 2
        )
        out = tmp_path / "r.json"
        assert (
            main(["run", "--corpus", str(CORPUS), "--only", "read-balance", "--out", str(out)]) == 0
        )
        assert main(["report", str(out), "--out", str(tmp_path / "no" / "x.md")]) == 2


class TestThirdImplementationReview:
    def test_a_sign_on_a_non_decodable_step_is_a_corpus_fault_on_both_paths(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad_step = {
            "tool": "open_transaction",
            "key": "zz",
            "arguments": {"typo_field": 1},
            "approval": {"sign": {"approval_id": "x", "expires_in_seconds": 10}},
        }
        root = _copy_corpus(tmp_path)
        p = root / "scenarios" / "correct" / "approval-granted.yaml"
        doc = yaml.safe_load(p.read_text())
        doc["agent"]["script"].append(bad_step)
        p.write_text(yaml.safe_dump(doc))
        with pytest.raises(CorpusError, match="does not decode"):
            load_corpus(root)
        root = _copy_corpus(tmp_path)
        p = root / "scenarios" / "correct" / "approval-granted.yaml"
        doc = yaml.safe_load(p.read_text())
        doc["setup"]["before"].append(bad_step)
        p.write_text(yaml.safe_dump(doc))
        target = tmp_path / "j"
        assert (
            main(["run", "--corpus", str(root), "--emit-setup", "approval-granted", str(target)])
            == 2
        )
        assert "does not decode" in capsys.readouterr().err
        assert not target.exists() and not target.with_name("j.policy.json").exists()

    def test_emit_setup_cleanup_is_unconditional(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ledgergate import runner

        corpus = load_corpus(CORPUS)
        sc = next(s for s in corpus.scenarios if s.id == "read-balance")

        def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("an exception outside every list")

        monkeypatch.setattr(runner, "_apply", boom)
        target = tmp_path / "j"
        with pytest.raises(RuntimeError):
            runner.emit_setup(sc, target)
        assert not target.exists() and not target.with_name("j.policy.json").exists()

    def test_only_is_deduplicated_in_the_selection(self) -> None:
        r = run(load_corpus(CORPUS), only=("read-balance", "read-balance"))
        assert r.selection.only == ("read-balance",)


class TestFourthImplementationReview:
    def test_non_object_arguments_are_a_corpus_fault_not_a_traceback(self, tmp_path: Path) -> None:
        root = _copy_corpus(tmp_path)
        p = root / "scenarios" / "correct" / "approval-granted.yaml"
        doc = yaml.safe_load(p.read_text())
        doc["agent"]["script"].append(
            {
                "tool": "open_transaction",
                "key": "zz",
                "arguments": [1, 2],
                "approval": {"sign": {"approval_id": "x", "expires_in_seconds": 10}},
            }
        )
        p.write_text(yaml.safe_dump(doc))
        with pytest.raises(CorpusError, match="arguments: Input should be a valid dictionary"):
            load_corpus(root)

    def test_keep_traces_failure_is_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        assert (
            main(
                [
                    "run",
                    "--corpus",
                    str(CORPUS),
                    "--only",
                    "read-balance",
                    "--keep-traces",
                    str(blocker),
                ]
            )
            == 2
        )
        assert "keep-traces" in capsys.readouterr().err

    def test_a_signed_reverse_by_entry_ref_validates_and_runs(self, tmp_path: Path) -> None:
        # the shipped policy never gates a reverse, so the artefact is `approval_not_applicable`;
        # what matters is that the step validates and the fingerprint derives from the resolved id
        root = _copy_corpus(tmp_path)
        p = root / "scenarios" / "correct" / "post-and-reverse.yaml"
        doc = yaml.safe_load(p.read_text())
        doc["agent"]["script"][1]["approval"] = {
            "sign": {"approval_id": "r", "expires_in_seconds": 10}
        }
        p.write_text(yaml.safe_dump(doc))
        r = run(load_corpus(root), only=("post-and-reverse",))
        assert r.scenarios[0].status in ("pass", "fail") and r.scenarios[0].error is None


class TestFifthImplementationReview:
    @pytest.mark.parametrize(
        ("mutate", "needle"),
        [
            (lambda d: d["setup"]["before"].append({"tool": {"x": 1}, "key": "k"}), "tool"),
            (lambda d: d["setup"]["before"].append({"tool": "post", "key": ["a"]}), "key"),
            (lambda d: d["setup"].__setitem__("started_at", "9999-12-31T23:59:59Z"), "started_at"),
            (
                lambda d: d["agent"]["script"][1]["approval"]["sign"].__setitem__(
                    "expires_in_seconds", 10**15
                ),
                "expires_in_seconds",
            ),
            (
                lambda d: d["agent"]["script"][0]["arguments"].__setitem__("entry_ref", "setup-1"),
                "only for reverse",
            ),
        ],
    )
    def test_every_step_shape_fault_is_a_corpus_fault(
        self, tmp_path: Path, mutate: Any, needle: str
    ) -> None:
        root = _copy_corpus(tmp_path)
        p = root / "scenarios" / "correct" / "approval-granted.yaml"
        doc = yaml.safe_load(p.read_text())
        doc["setup"]["before"] = [
            {
                "tool": "post",
                "key": "setup-1",
                "arguments": {
                    "draft": {
                        "postings": [
                            {
                                "account": "fees",
                                "side": "debit",
                                "money": {"amount": 5, "currency": "USD"},
                            },
                            {
                                "account": "cash",
                                "side": "credit",
                                "money": {"amount": 5, "currency": "USD"},
                            },
                        ]
                    }
                },
            }
        ]
        mutate(doc)
        p.write_text(yaml.safe_dump(doc))
        with pytest.raises(CorpusError, match=needle):
            load_corpus(root)

    def test_the_result_records_signed_steps(self) -> None:
        r = run(load_corpus(CORPUS), only=("approval-granted", "read-balance"))
        by_id = {s.id: s for s in r.scenarios}
        assert by_id["approval-granted"].signed == ("agent-2",)
        assert by_id["read-balance"].signed == ()


class TestSixthImplementationReview:
    @pytest.mark.parametrize(
        ("mutate", "needle"),
        [
            (
                lambda d: d["setup"].__setitem__("started_at", "0001-01-01T00:00:00+05:00"),
                "started_at",
            ),
            (
                lambda d: d["setup"]["policy"]["window_caps"][0].__setitem__("window", 10**30),
                "setup.policy",
            ),
            (
                lambda d: d["setup"].__setitem__("currencies", [{"code": "USD", "exponent": 3}]),
                "redeclares",
            ),
        ],
    )
    def test_more_inputs_that_must_be_corpus_faults(
        self, tmp_path: Path, mutate: Any, needle: str
    ) -> None:
        root = _copy_corpus(tmp_path)
        p = root / "scenarios" / "correct" / "read-balance.yaml"
        doc = yaml.safe_load(p.read_text())
        mutate(doc)
        p.write_text(yaml.safe_dump(doc))
        with pytest.raises(CorpusError, match=needle):
            load_corpus(root)

    def test_duplicate_yaml_keys_and_deep_nesting_are_corpus_faults(self, tmp_path: Path) -> None:
        root = _copy_corpus(tmp_path)
        p = root / "expectations" / "read-balance.yaml"
        p.write_text(p.read_text() + "ledger_commands: 5\nledger_commands: 0\n")
        with pytest.raises(CorpusError, match="duplicate key"):
            load_corpus(root)
        root = _copy_corpus(tmp_path)
        p = root / "scenarios" / "correct" / "read-balance.yaml"
        text = p.read_text().replace(
            "arguments:\n      account: cash",
            "arguments:\n      account: " + "[" * 5000 + "]" * 5000,
        )
        assert text != p.read_text()
        p.write_text(text)
        with pytest.raises(CorpusError):
            load_corpus(root)


class TestSeventhImplementationReview:
    def test_complex_yaml_keys_non_utf8_and_merge_keys(self, tmp_path: Path) -> None:
        for tail in ("? [1, 2]\n: 3\n", "? {a: 1}\n: 3\n"):
            root = _copy_corpus(tmp_path)
            p = root / "expectations" / "read-balance.yaml"
            p.write_text(p.read_text() + tail)
            with pytest.raises(CorpusError, match="unhashable"):
                load_corpus(root)
        root = _copy_corpus(tmp_path)
        (root / "expectations" / "read-balance.yaml").write_bytes(b"\xff\xfe" + b"id: x\n")
        with pytest.raises(CorpusError, match="UnicodeDecodeError"):
            load_corpus(root)
        # a merge key resolves as the standard constructor would (then extra=forbid decides)
        root = _copy_corpus(tmp_path)
        p = root / "expectations" / "read-balance.yaml"
        doc = yaml.safe_load(p.read_text())
        p.write_text(
            "base: &b {invocations: 1}\n"
            + yaml.safe_dump(doc).replace("invocations: 1\n", "<<: *b\n")
        )
        with pytest.raises(CorpusError, match="base"):  # `base` itself is the extra key
            load_corpus(root)

    def test_deep_supplied_trace_is_an_unreadable_trace_row(self, tmp_path: Path) -> None:
        traces = tmp_path / "t"
        traces.mkdir()
        (traces / "read-balance.json").write_text("[" * 200_000)
        r = run(load_corpus(CORPUS), only=("read-balance",), traces=traces)
        assert r.scenarios[0].error is not None and r.scenarios[0].error.startswith(
            "unreadable trace"
        )
        (traces / "read-balance.json").write_bytes(b"\xff\xfe{")
        r = run(load_corpus(CORPUS), only=("read-balance",), traces=traces)
        assert r.scenarios[0].error is not None and r.scenarios[0].error.startswith(
            "unreadable trace"
        )

    def test_fractional_thresholds_are_refused_not_truncated(self, tmp_path: Path) -> None:
        root = _copy_corpus(tmp_path)
        p = root / "scenarios" / "correct" / "read-balance.yaml"
        doc = yaml.safe_load(p.read_text())
        doc["setup"]["policy"]["window_caps"][0]["amount"] = 1.5
        p.write_text(yaml.safe_dump(doc))
        with pytest.raises(CorpusError, match="whole number"):
            load_corpus(root)


class TestEighthImplementationReview:
    def test_a_model_valid_trace_with_an_unsafe_integer_is_refused_at_load(
        self, tmp_path: Path
    ) -> None:
        traces = tmp_path / "t"
        run(load_corpus(CORPUS), only=("read-balance",), keep_traces=traces)
        doc = json.loads((traces / "read-balance.json").read_text())
        for e in doc["events"]:
            if (
                e["type"] in ("tool_call", "read_intent")
                and e.get("arguments", {}).get("account") == "cash"
            ):
                e["arguments"]["pad"] = 10**20
        (traces / "read-balance.json").write_text(json.dumps(doc))
        r = run(load_corpus(CORPUS), only=("read-balance",), traces=traces)
        assert r.scenarios[0].status == "error"
        assert r.scenarios[0].error is not None and r.scenarios[0].error.startswith(
            "unreadable trace"
        )
        # result.json is written whatever the trace did
        out = tmp_path / "r.json"
        assert (
            main(
                [
                    "run",
                    "--corpus",
                    str(CORPUS),
                    "--only",
                    "read-balance",
                    "--traces",
                    str(traces),
                    "--out",
                    str(out),
                ]
            )
            == 1
        )
        assert out.exists()


class TestNinthImplementationReview:
    def test_model_valid_traces_that_break_scoring_are_unreadable_rows(
        self, tmp_path: Path
    ) -> None:
        traces = tmp_path / "t"
        run(load_corpus(CORPUS), only=("refund-within-cap",), keep_traces=traces)
        base_doc = json.loads((traces / "refund-within-cap.json").read_text())
        decisions = [e for e in base_doc["events"] if e["type"] == "policy_decision"]
        agent_decision = decisions[-1]
        assert agent_decision["context"]["aggregates"], "the refund decision carries an aggregate"
        # an evaluation time at the calendar's edge: valid to the model, arithmetic must not escape
        doc = json.loads(json.dumps(base_doc))
        d = [e for e in doc["events"] if e["type"] == "policy_decision"][-1]
        d["context"]["evaluated_at"] = "0001-01-01T00:00:00+00:00"
        (traces / "refund-within-cap.json").write_text(json.dumps(doc))
        out = tmp_path / "r.json"
        code = main(
            [
                "run",
                "--corpus",
                str(CORPUS),
                "--only",
                "refund-within-cap",
                "--traces",
                str(traces),
                "--out",
                str(out),
            ]
        )
        assert code == 1 and out.exists()
        row = load_result(out.read_text()).scenarios[0]
        assert row.status in ("fail", "error")  # scored honestly or refused, never a traceback
        # a window no set could define is a forged input, reported by the registry, not arithmetic
        doc = json.loads(json.dumps(base_doc))
        d = [e for e in doc["events"] if e["type"] == "policy_decision"][-1]
        ((_name, value),) = d["context"]["aggregates"].items()
        d["context"]["aggregates"] = {"applied.refund.USD.9999999999s": value}
        (traces / "refund-within-cap.json").write_text(json.dumps(doc))
        r = run(load_corpus(CORPUS), only=("refund-within-cap",), traces=traces)
        assert r.scenarios[0].status == "fail"
        assert r.scenarios[0].scorecard is not None
        assert any(
            i.name == "decision_recomputes" and i.status == "fail"
            for i in r.scenarios[0].scorecard.invariants
        )
        # an eleven-digit window is outside the grammar altogether
        d["context"]["aggregates"] = {"applied.refund.USD.99999999999s": value}
        (traces / "refund-within-cap.json").write_text(json.dumps(doc))
        r = run(load_corpus(CORPUS), only=("refund-within-cap",), traces=traces)
        assert r.scenarios[0].error is not None and r.scenarios[0].error.startswith(
            "unreadable trace"
        )
