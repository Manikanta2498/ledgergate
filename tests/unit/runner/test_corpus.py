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
        doc["summary"] = _resummarize(doc["scenarios"])
        with pytest.raises(ResultError, match="outside the vocabulary"):
            render_sarif(load_result(json.dumps(doc)))
        other = dict(doc)
        other["scenarios"] = []
        other["summary"] = _resummarize([])
        with pytest.raises(ResultError, match="same scenario ids"):
            drift(r, load_result(json.dumps(other)))

    def test_emit_setup_failure_leaves_no_orphan_policy_file(self, tmp_path: Path) -> None:
        from ledgergate.runner import emit_setup

        corpus = load_corpus(CORPUS)
        sc = next(s for s in corpus.scenarios if s.id == "read-balance")
        target = tmp_path / "nodir" / "j"  # parent missing: journal creation fails after policy
        with pytest.raises((CorpusError, Exception)):
            emit_setup(sc, target)
        assert not target.with_name(target.name + ".policy.json").exists()
