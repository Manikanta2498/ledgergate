"""Conformance levels, docs/spec/assurance.md: a rendering of the result document(s)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ledgergate.cli.__main__ import main
from ledgergate.report import (
    ResultError,
    ScenarioResult,
    conformance,
    dump_result,
    load_result,
    render_markdown,
    summarize,
)
from ledgergate.runner import load_corpus, run

CORPUS = Path("corpus")


def _edit(result: Any, **changes: Any) -> Any:
    doc = json.loads(dump_result(result))
    for sid, fields in changes.items():
        for s in doc["scenarios"]:
            if s["id"] == sid:
                s.update(fields)
    doc["summary"] = summarize(
        [ScenarioResult.model_validate(s) for s in doc["scenarios"]]
    ).model_dump(mode="json", by_alias=True)
    return load_result(json.dumps(doc))


class TestLevels:
    def test_shipped_corpus_is_l2_and_l3_against_itself(self) -> None:
        r = run(load_corpus(CORPUS))
        c = conformance(r)
        assert c.line == "L2 (22 scenarios, 13 red-team; no baseline)"
        assert conformance(r, r).line == "L3 (22 scenarios, 13 red-team)"
        assert (
            render_markdown(r).splitlines()[2]
            == "**Conformance: L2 (22 scenarios, 13 red-team; no baseline)**"
        )

    def test_reasons_follow_the_fixed_order_and_every_reason_that_holds(self) -> None:
        r = run(load_corpus(CORPUS))
        failed = _edit(
            r, **{"refund-over-cap": {"status": "fail"}, "read-balance": {"status": "fail"}}
        )
        assert conformance(failed).line == (
            "L0 (22 scenarios, 13 red-team; correct failed: read-balance;"
            " red-team failed: refund-over-cap; no baseline)"
        )
        red_only = _edit(r, **{"refund-over-cap": {"status": "fail"}})
        assert conformance(red_only).level == "L1"
        unscored: dict[str, Any] = {
            "status": "skipped",
            "source": "none",
            "trace_digest": None,
            "scorecard": None,
            "expectations": [],
            "signed": [],
        }
        all_skipped = _edit(r, **{s.id: unscored for s in r.scenarios})
        assert conformance(all_skipped).line == (
            "L0 (22 scenarios, 13 red-team; skipped: 22; nothing scored; no baseline)"
        )
        partial = run(load_corpus(CORPUS), kind="correct")
        assert (
            conformance(partial).line
            == "L0 (9 scenarios, 0 red-team; partial selection; no red-team; no baseline)"
        )

    def test_two_documents(self) -> None:
        r = run(load_corpus(CORPUS))
        base_l1 = _edit(r, **{"refund-over-cap": {"status": "fail"}})
        c = conformance(r, base_l1)
        assert c.level == "L2" and "baseline: L1" in c.reasons
        doc = json.loads(dump_result(r))
        doc["scenarios"][0]["trace_digest"] = "0" * 64
        changed = load_result(json.dumps(doc))
        c = conformance(changed, r)
        assert c.level == "L2" and c.reasons[-1] == f"trace changed: {doc['scenarios'][0]['id']}"
        other = json.loads(dump_result(r))
        other["corpus_digest"] = "1" * 64
        with pytest.raises(ResultError, match="different corpora"):
            conformance(r, load_result(json.dumps(other)))


class TestCli:
    def test_exit_codes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        out = tmp_path / "r.json"
        assert main(["run", "--corpus", str(CORPUS), "--out", str(out)]) == 0
        assert main(["report", "--conformance", str(out)]) == 0
        assert capsys.readouterr().out.startswith("L2 (")
        assert main(["report", "--conformance", str(out), "--require", "L3"]) == 2  # no baseline
        assert main(["report", "--conformance", str(out), "--require", "L2"]) == 0
        assert (
            main(["report", "--conformance", str(out), "--baseline", str(out), "--require", "L3"])
            == 0
        )
        partial = tmp_path / "p.json"
        assert (
            main(["run", "--corpus", str(CORPUS), "--kind", "correct", "--out", str(partial)]) == 0
        )
        assert main(["report", "--conformance", str(partial)]) == 0  # a rendering
        assert main(["report", "--conformance", str(partial), "--require", "L1"]) == 1
        assert (
            main(["report", "--conformance", str(out), "--baseline", str(partial)]) == 2
        )  # preconditions
        assert main(["report", "--conformance", str(out), str(out)]) == 2
        assert main(["report", "--conformance", str(tmp_path / "missing.json")]) == 2
