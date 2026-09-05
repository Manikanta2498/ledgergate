# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The result document (``schema/result/v1.json``), its renderings and the drift table, to
``docs/spec/corpus.md``. Every renderer is a pure function of the result document(s); this
package learns the rule set from the document, never from the invariant registry."""

from __future__ import annotations

import json
from typing import Any, Literal
from xml.sax.saxutils import escape, quoteattr

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

Status = Literal["pass", "fail", "error", "skipped"]
Kind = Literal["correct", "red-team"]
Source = Literal["script", "trace", "none"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FindingDoc(_Strict):
    severity: str
    intent_id: str | None = None
    message: str


class InvariantDoc(_Strict):
    name: str
    status: str
    findings: tuple[FindingDoc, ...] = ()


class ScorecardDoc(_Strict):
    status: str
    passed: bool
    intents: int
    ledger_commands: int
    invariants: tuple[InvariantDoc, ...]


class ExpectationDoc(_Strict):
    key: str
    status: Literal["pass", "fail"]
    expected: Any
    actual: Any


RUNNER_ERRORS = ("setup mismatch", "unreadable trace", "unresolved entry_ref", "journal refused")
"""The closed vocabulary of runner error messages (corpus.md); a document outside it does not
load, so every renderer fails closed by construction."""


class ScenarioResult(_Strict):
    id: str
    kind: Kind
    title: str
    status: Status
    source: Source
    trace_digest: str | None = None
    scorecard: ScorecardDoc | None = None
    expectations: tuple[ExpectationDoc, ...] = ()
    signed: tuple[str, ...] = ()  # the script steps the runner signed artefacts for
    error: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> ScenarioResult:
        if (self.status == "error") != (self.error is not None):
            raise ValueError("error is present exactly for an error scenario")
        if self.error is not None and not self.error.startswith(RUNNER_ERRORS):
            raise ValueError("runner error outside the vocabulary")
        if self.status in ("error", "skipped") and (
            self.trace_digest is not None or self.scorecard is not None or self.expectations
        ):
            raise ValueError("an unscored scenario carries no digest, scorecard or expectations")
        if self.status in ("pass", "fail") and (
            self.trace_digest is None or self.scorecard is None
        ):
            raise ValueError("a scored scenario carries a digest and a scorecard")
        return self


class KindSummary(_Strict):
    scenarios: int
    pass_: int = Field(alias="pass")
    fail: int
    error: int
    skipped: int

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Summary(_Strict):
    scenarios: int
    pass_: int = Field(alias="pass")
    fail: int
    error: int
    skipped: int
    by_kind: dict[str, KindSummary]

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Selection(_Strict):
    only: tuple[str, ...] = ()
    kind: Kind | None = None


class Result(_Strict):
    schema_version: Literal["1"] = "1"
    ledgergate_version: str
    corpus_digest: str
    summary: Summary
    selection: Selection
    scenarios: tuple[ScenarioResult, ...]

    @model_validator(mode="after")
    def _consistent(self) -> Result:
        if summarize(list(self.scenarios)) != self.summary:
            raise ValueError("summary does not recount the scenarios")
        return self

    @property
    def gate(self) -> int:
        """run's exit: 0 all scored passed, 1 any fail/error, 3 nothing scored."""
        scored = [s for s in self.scenarios if s.status != "skipped"]
        if not scored:
            return 3
        return 0 if all(s.status == "pass" for s in scored) else 1


def summarize(scenarios: list[ScenarioResult]) -> Summary:
    def count(items: list[ScenarioResult]) -> dict[str, int]:
        return {
            "scenarios": len(items),
            "pass": sum(s.status == "pass" for s in items),
            "fail": sum(s.status == "fail" for s in items),
            "error": sum(s.status == "error" for s in items),
            "skipped": sum(s.status == "skipped" for s in items),
        }

    by_kind = {
        kind: KindSummary.model_validate(count([s for s in scenarios if s.kind == kind]))
        for kind in ("correct", "red-team")
    }
    return Summary.model_validate({**count(scenarios), "by_kind": by_kind})


def dump_result(result: Result) -> str:
    return (
        json.dumps(
            result.model_dump(mode="json", by_alias=True),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


class ResultError(ValueError):
    pass


def load_result(text: str) -> Result:
    try:
        return Result.model_validate_json(text)
    except ValidationError as exc:
        raise ResultError(
            "; ".join(f"{e['type']}: {e['msg']}" for e in exc.errors(include_input=False))
        ) from exc


def json_schema() -> dict[str, Any]:
    schema = Result.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://ledgergate.dev/schema/result/v1.json"
    return schema


# ------------------------------------------------------------------ renderers


def render_markdown(result: Result) -> str:
    s = result.summary
    lines = [
        "# LedgerGate corpus result",
        "",
        f"Corpus `{result.corpus_digest[:12]}`, ledgergate {result.ledgergate_version}.",
        "",
        f"**{s.pass_} pass, {s.fail} fail, {s.error} error, {s.skipped} skipped**"
        f" of {s.scenarios}.",
        "",
        "| Scenario | Kind | Status | Detail |",
        "| :-- | :-- | :-- | :-- |",
    ]
    for sc in result.scenarios:
        detail = _detail(sc)
        lines.append(f"| `{sc.id}` | {sc.kind} | {sc.status} | {detail} |")
    return "\n".join(lines) + "\n"


def _detail(sc: ScenarioResult) -> str:
    if sc.status == "error":
        return sc.error or "error"
    if sc.status == "skipped":
        return "no trace"
    failing = [e.key for e in sc.expectations if e.status == "fail"]
    if sc.scorecard is not None and sc.scorecard.status == "fail":
        failing.insert(0, "invariants")
    return ", ".join(failing) if failing else ""


def render_junit(result: Result) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<testsuites>"]
    for kind in ("correct", "red-team"):
        ks = result.summary.by_kind[kind]
        out.append(
            f'  <testsuite name={quoteattr(kind)} tests="{ks.scenarios}" failures="{ks.fail}"'
            f' errors="{ks.error}" skipped="{ks.skipped}" time="0">'
        )
        for sc in result.scenarios:
            if sc.kind != kind:
                continue
            head = f'    <testcase classname={quoteattr(kind)} name={quoteattr(sc.id)} time="0"'
            if sc.status == "pass":
                out.append(head + " />")
                continue
            out.append(head + ">")
            if sc.status == "fail":
                out.append(
                    f"      <failure message={quoteattr(_detail(sc))}>"
                    f"{escape(_detail(sc))}</failure>"
                )
            elif sc.status == "error":
                out.append(
                    f"      <error message={quoteattr(sc.error or 'error')}>"
                    f"{escape(sc.error or 'error')}</error>"
                )
            else:
                out.append("      <skipped />")
            out.append("    </testcase>")
        out.append("  </testsuite>")
    out.append("</testsuites>")
    return "\n".join(out) + "\n"


_RUNNER_RULES = {
    "setup mismatch": "runner/setup-mismatch",
    "unreadable trace": "runner/unreadable-trace",
    "unresolved entry_ref": "runner/unresolved-entry-ref",
    "journal refused": "runner/journal-refused",
}


def _runner_rule(error: str | None) -> str:
    # the model already refused anything outside RUNNER_ERRORS; this cannot miss
    return next(
        rule for prefix, rule in _RUNNER_RULES.items() if error and error.startswith(prefix)
    )


def render_sarif(result: Result) -> str:
    rule_ids: list[str] = []
    for sc in result.scenarios:
        if sc.scorecard is not None:
            for inv in sc.scorecard.invariants:
                if inv.name not in rule_ids:
                    rule_ids.append(inv.name)
        for e in sc.expectations:
            rid = f"expectation/{e.key}"
            if rid not in rule_ids:
                rule_ids.append(rid)
    rule_ids.extend(sorted(set(_RUNNER_RULES.values())))
    results: list[dict[str, Any]] = []
    notifications: list[dict[str, Any]] = []
    for sc in result.scenarios:
        artifact = {
            "artifactLocation": {
                "uri": f"corpus/scenarios/{sc.kind}/{sc.id}.yaml",
                "uriBaseId": "%SRCROOT%",
            }
        }
        if sc.status == "skipped":
            notifications.append(
                {"level": "note", "message": {"text": f"{sc.id}: no trace supplied; not scored"}}
            )
            continue
        if sc.status == "error":
            results.append(
                {
                    "ruleId": _runner_rule(sc.error),
                    "level": "error",
                    "message": {"text": f"{sc.id}: {sc.error}"},
                    "locations": [{"physicalLocation": artifact}],
                }
            )
            continue
        if sc.scorecard is not None:
            for inv in sc.scorecard.invariants:
                if inv.status != "fail":
                    continue
                for f in inv.findings:
                    logical = f.intent_id if f.intent_id is not None else sc.id
                    results.append(
                        {
                            "ruleId": inv.name,
                            "level": "error",
                            "message": {"text": f.message},
                            "locations": [
                                {
                                    "physicalLocation": artifact,
                                    "logicalLocations": [{"name": logical}],
                                }
                            ],
                        }
                    )
        for e in sc.expectations:
            if e.status == "fail":
                results.append(
                    {
                        "ruleId": f"expectation/{e.key}",
                        "level": "error",
                        "message": {
                            "text": f"{sc.id}: expected {json.dumps(e.expected, sort_keys=True)},"
                            f" actual {json.dumps(e.actual, sort_keys=True)}"
                        },
                        "locations": [{"physicalLocation": artifact}],
                    }
                )
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ledgergate",
                        "version": result.ledgergate_version,
                        "rules": [{"id": rid} for rid in rule_ids],
                    }
                },
                "invocations": [
                    {"executionSuccessful": True, "toolExecutionNotifications": notifications}
                ],
                "results": results,
            }
        ],
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


# ------------------------------------------------------------------ drift

Bucket = Literal["regressed", "fixed", "unchanged", "changed", "newly_skipped", "newly_scored"]


class DriftRow(_Strict):
    id: str
    baseline: Status
    candidate: Status
    bucket: Bucket
    same_trace: bool | None  # None unless scored in both


class Drift(_Strict):
    corpus_digest: str
    rows: tuple[DriftRow, ...]
    allowed_newly_skipped: tuple[str, ...] = ()

    @property
    def gate(self) -> int:
        for r in self.rows:
            if r.bucket == "regressed":
                return 1
            if r.bucket == "newly_skipped" and r.id not in self.allowed_newly_skipped:
                return 1
            if r.bucket == "newly_scored" and r.candidate != "pass":
                return 1
        return 0


def _bucket(b: Status, c: Status) -> Bucket:
    if b == c:
        return "unchanged"
    if b == "skipped":
        return "newly_scored"
    if c == "skipped":
        return "newly_skipped"
    if b == "pass":
        return "regressed"
    if c == "pass":
        return "fixed"
    return "changed"


def drift(baseline: Result, candidate: Result, *, allow_newly_skipped: bool = False) -> Drift:
    if baseline.corpus_digest != candidate.corpus_digest:
        raise ResultError("results are of different corpora; comparing them is noise, not drift")
    if baseline.selection != candidate.selection:
        raise ResultError("results have different selections; every id must be in both")
    if baseline.ledgergate_version != candidate.ledgergate_version:
        raise ResultError(
            "results are from different ledgergate versions; a changed digest would be the"
            " runtime's, not the agent's"
        )
    by_c = {s.id: s for s in candidate.scenarios}
    if set(by_c) != {s.id for s in baseline.scenarios}:
        raise ResultError("results do not cover the same scenario ids")
    rows = []
    for b in baseline.scenarios:
        c = by_c[b.id]
        same = None
        if (
            b.status != "skipped"
            and c.status != "skipped"
            and b.status != "error"
            and c.status != "error"
        ):
            same = b.trace_digest == c.trace_digest
        rows.append(
            DriftRow(
                id=b.id,
                baseline=b.status,
                candidate=c.status,
                bucket=_bucket(b.status, c.status),
                same_trace=same,
            )
        )
    allowed = (
        tuple(r.id for r in rows if r.bucket == "newly_skipped") if allow_newly_skipped else ()
    )
    return Drift(
        corpus_digest=baseline.corpus_digest, rows=tuple(rows), allowed_newly_skipped=allowed
    )


def render_drift_markdown(d: Drift) -> str:
    lines = [
        "# LedgerGate drift",
        "",
        f"Corpus `{d.corpus_digest[:12]}`.",
        "",
        "| Scenario | Baseline | Candidate | Bucket | Trace |",
        "| :-- | :-- | :-- | :-- | :-- |",
    ]
    for r in d.rows:
        trace = "" if r.same_trace is None else ("same" if r.same_trace else "changed")
        bucket = r.bucket + (" (allowed)" if r.id in d.allowed_newly_skipped else "")
        lines.append(f"| `{r.id}` | {r.baseline} | {r.candidate} | {bucket} | {trace} |")
    return "\n".join(lines) + "\n"


def render_drift_json(d: Drift) -> str:
    return json.dumps(d.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


__all__ = [
    "Drift",
    "DriftRow",
    "ExpectationDoc",
    "FindingDoc",
    "InvariantDoc",
    "KindSummary",
    "Result",
    "ResultError",
    "ScenarioResult",
    "ScorecardDoc",
    "Selection",
    "Summary",
    "drift",
    "dump_result",
    "json_schema",
    "load_result",
    "render_drift_json",
    "render_drift_markdown",
    "render_junit",
    "render_markdown",
    "render_sarif",
    "summarize",
]
