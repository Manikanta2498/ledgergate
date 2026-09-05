# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The corpus runner, to ``docs/spec/corpus.md``: load and validate a corpus, produce a
trace per scenario (scripted through a Journal, or supplied), bind it to the setup, score
the invariants and the expectations, and emit the result document."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ledgergate import __version__
from ledgergate.codec import canonical_text, decode_command, digest
from ledgergate.invariants import Scorecard, check
from ledgergate.journal import (
    IdentityAdmitter,
    Journal,
    JournalError,
    NullPolicySet,
    ThresholdPolicySet,
    issue,
    signing_key_from_bytes,
    verification_key_text,
)
from ledgergate.journal.approvals import _unb64 as unb64
from ledgergate.ledger import (
    CURRENCIES,
    ChartOfAccounts,
    SequentialIds,
    SteppingClock,
    command_fingerprint,
)
from ledgergate.ledger.identifiers import require_identifier
from ledgergate.report import (
    ExpectationDoc,
    Result,
    ScenarioResult,
    ScorecardDoc,
    Selection,
    summarize,
)
from ledgergate.trace import TraceError, load_any, replay_trace
from ledgergate.trace.models import (
    AccountDoc,
    CurrencyDoc,
    LedgerCommandEvent,
    LedgerResultEvent,
    ToolCallEvent,
)
from ledgergate.trace.v2 import InvocationResolution, PolicyDecision, TraceV2

Kind = Literal["correct", "red-team"]
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
OUTCOMES = ("applied", "rejected", "denied", "awaiting_approval")
DISPOSITIONS = ("new", "replay", "conflict", "approval", "read", "invalid")


class CorpusError(ValueError):
    """A corpus fault: exit 2, nothing scored."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SignSpec(_Strict):
    approval_id: str
    expires_in_seconds: int = Field(ge=0)
    approver: str | None = None
    journal_id: str | None = None
    fingerprint: str | None = None
    key: str | None = None


class Step(_Strict):
    tool: Any
    arguments: Any = None
    key: Any = None
    approval: Any = None

    def request(self, call_id: str) -> dict[str, Any]:
        value: dict[str, Any] = {"tool": self.tool, "call_id": call_id}
        if self.arguments is not None:
            value["arguments"] = self.arguments
        if self.key is not None:
            value["key"] = self.key
        return value


class Approvals(_Strict):
    signing_key: str
    approver: str = "approver"


class Setup(_Strict):
    started_at: datetime
    chart: tuple[AccountDoc, ...]
    currencies: tuple[CurrencyDoc, ...] = ()
    policy: dict[str, Any] | None = None
    approvals: Approvals | None = None
    before: tuple[Step, ...] = ()


class Attachment(_Strict):
    role: str
    content: str


class Task(_Strict):
    instruction: str
    attachments: tuple[Attachment, ...] = ()


class Agent(_Strict):
    script: tuple[Step, ...] | None = None


class Scenario(_Strict):
    schema_version: Literal["1"]
    id: str
    kind: Kind
    title: str
    description: str = ""
    scripted_only: bool = False
    setup: Setup
    task: Task
    agent: Agent = Agent()


class Expectations(_Strict):
    schema_version: Literal["1"]
    id: str
    status: Literal["pass", "no_evidence"] | None = None
    dispositions: dict[str, int] | None = None
    outcomes: dict[str, int] | None = None
    matched_rules: dict[str, int] | None = None
    balances: dict[str, str] | None = None
    ledger_commands: int | None = None
    invocations: int | None = None


@dataclass(frozen=True)
class Corpus:
    root: Path
    scenarios: tuple[Scenario, ...]
    expectations: Mapping[str, Expectations]
    digest: str


# ------------------------------------------------------------------ loading


def _yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CorpusError(f"{path}: cannot read: {type(exc).__name__}") from exc


def _validate(model: type[BaseModel], doc: Any, path: Path) -> Any:
    try:
        return model.model_validate(doc)
    except ValidationError as exc:
        first = exc.errors(include_input=False)[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise CorpusError(f"{path}: {loc}: {first['msg']}") from exc


def load_corpus(root: Path) -> Corpus:
    if not (root / "scenarios").is_dir() or not (root / "expectations").is_dir():
        raise CorpusError(f"{root}: not a corpus (needs scenarios/ and expectations/)")
    scenarios: list[Scenario] = []
    files: list[Path] = []
    for kind in ("correct", "red-team"):
        for path in sorted((root / "scenarios" / kind).glob("*.yaml")):
            files.append(path)
            sc = _validate(Scenario, _yaml(path), path)
            if sc.kind != kind:
                raise CorpusError(f"{path}: kind {sc.kind!r} does not match directory {kind!r}")
            if sc.id != path.stem or not _ID.fullmatch(sc.id):
                raise CorpusError(f"{path}: id must equal the file stem and match the grammar")
            _validate_scenario(sc, path)
            scenarios.append(sc)
    ids = [s.id for s in scenarios]
    if len(set(ids)) != len(ids):
        raise CorpusError(f"{root}: duplicate scenario id across kinds")
    expectations: dict[str, Expectations] = {}
    for path in sorted((root / "expectations").glob("*.yaml")):
        files.append(path)
        ex = _validate(Expectations, _yaml(path), path)
        if ex.id != path.stem:
            raise CorpusError(f"{path}: id must equal the file stem")
        _validate_expectations(ex, path)
        expectations[ex.id] = ex
    missing = sorted(set(ids) - set(expectations))
    orphans = sorted(set(expectations) - set(ids))
    if missing or orphans:
        raise CorpusError(
            f"{root}: scenarios without expectations {missing};"
            f" expectations without scenarios {orphans}"
        )
    if not scenarios:
        raise CorpusError(f"{root}: empty corpus")
    entries = [
        {
            "path": p.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(files)
    ]
    return Corpus(root, tuple(scenarios), expectations, digest(entries))


def _validate_scenario(sc: Scenario, path: Path) -> None:
    try:
        require_identifier(sc.id, "id")
    except Exception as exc:
        raise CorpusError(f"{path}: {exc}") from exc
    if sc.setup.started_at.tzinfo is None:
        raise CorpusError(f"{path}: setup.started_at must carry a timezone")
    policy = _policy(sc.setup.policy, path)
    if getattr(policy, "approve_above", ()) and sc.setup.approvals is None:
        raise CorpusError(f"{path}: policy can require approval but setup has no approvals key")
    capped = {c.kind for c in getattr(policy, "window_caps", ())}
    if capped and any(s.tool in capped for s in sc.setup.before) and not sc.scripted_only:
        raise CorpusError(f"{path}: window_caps and a capped write in before require scripted_only")
    if sc.scripted_only and sc.agent.script is None:
        raise CorpusError(f"{path}: scripted_only without agent.script can never be scored")
    if sc.setup.approvals is not None:
        try:
            signing_key_from_bytes(unb64(sc.setup.approvals.signing_key))
        except Exception as exc:
            raise CorpusError(f"{path}: approvals.signing_key is not an Ed25519 seed") from exc
    steps = list(sc.setup.before) + list(sc.agent.script or ())
    names = [f"setup-{i + 1}" for i in range(len(sc.setup.before))] + [
        f"agent-{i + 1}" for i in range(len(sc.agent.script or ()))
    ]
    for n, step in enumerate(steps):
        if isinstance(step.approval, dict) and "sign" in step.approval:
            if sc.setup.approvals is None:
                raise CorpusError(f"{path}: step {names[n]} signs but setup has no approvals")
            _validate(SignSpec, step.approval["sign"], path)
        args = step.arguments if isinstance(step.arguments, dict) else {}
        if "entry_ref" in args:
            if "entry_id" in args:
                raise CorpusError(f"{path}: step {names[n]} carries both entry_ref and entry_id")
            ref = args["entry_ref"]
            if ref not in names[:n]:
                raise CorpusError(
                    f"{path}: step {names[n]} entry_ref {ref!r} names no earlier step"
                )


def _validate_expectations(ex: Expectations, path: Path) -> None:
    for key, allowed in (("dispositions", DISPOSITIONS), ("outcomes", OUTCOMES)):
        values = getattr(ex, key)
        if values is not None:
            bad = sorted(set(values) - set(allowed))
            if bad:
                raise CorpusError(f"{path}: {key} names unknown kinds {bad}")


def _policy(doc: dict[str, Any] | None, path: Path) -> Any:
    if doc is None:
        return NullPolicySet()
    try:
        return ThresholdPolicySet.from_configuration(doc)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CorpusError(f"{path}: setup.policy: {type(exc).__name__}: {exc}") from exc


# ------------------------------------------------------------------ running a script


class PeekClock:
    """A stepping clock whose next reading can be read without advancing: the runner signs
    approvals at the reading the write will take (journal.md, write step 4)."""

    def __init__(self, start: datetime) -> None:
        self._inner = SteppingClock(start)

    @property
    def next(self) -> datetime:
        return self._inner._next

    def now(self) -> datetime:
        return self._inner.now()


class UnresolvedEntryRefError(Exception):
    pass


def _setup_journal(sc: Scenario, path: str) -> tuple[Journal, PeekClock]:
    clock = PeekClock(sc.setup.started_at)
    registry = dict(CURRENCIES)
    registry.update((c.code, c.to_currency()) for c in sc.setup.currencies)
    chart = ChartOfAccounts(a.to_account(registry) for a in sc.setup.chart)
    key = "none"
    if sc.setup.approvals is not None:
        key = verification_key_text(signing_key_from_bytes(unb64(sc.setup.approvals.signing_key)))
    journal = Journal.create(
        path,
        chart,
        clock=clock,
        ids=SequentialIds(),
        currencies={c.code: c.to_currency() for c in sc.setup.currencies} or None,
        admitter=IdentityAdmitter(),
        policy=_policy(sc.setup.policy, Path(sc.id)),
        approval_key=key,
    )
    return journal, clock


def _apply(
    journal: Journal,
    clock: PeekClock,
    sc: Scenario,
    steps: tuple[Step, ...],
    prefix: str,
    entries: dict[str, str],
    signed: list[str],
) -> None:
    for n, step in enumerate(steps, start=1):
        call_id = f"{prefix}-{n}"
        value = step.request(call_id)
        args = value.get("arguments")
        if isinstance(args, dict) and "entry_ref" in args:
            ref = args["entry_ref"]
            if ref not in entries:
                raise UnresolvedEntryRefError(ref)
            args = {**args}
            args["entry_id"] = entries[args.pop("entry_ref")]
            value["arguments"] = args
        if step.approval is not None:
            value["approval"] = _artefact(journal, clock, sc, step, call_id, signed)
        response = journal.handle(value)
        if response.ok and response.result is not None and "entry_id" in response.result:
            entries[call_id] = response.result["entry_id"]


def _artefact(
    journal: Journal, clock: PeekClock, sc: Scenario, step: Step, call_id: str, signed: list[str]
) -> Any:
    if not (isinstance(step.approval, dict) and "sign" in step.approval):
        return step.approval  # a literal artefact, passed as given (a forgery, usually)
    assert sc.setup.approvals is not None
    spec = SignSpec.model_validate(step.approval["sign"])
    private = signing_key_from_bytes(unb64(sc.setup.approvals.signing_key))
    key = spec.key or step.key
    fingerprint = spec.fingerprint
    if fingerprint is None:
        # as `ledgergate approve` derives it: the fingerprint of the command being presented
        registry = journal.definition.registry
        doc = {"kind": step.tool, "key": step.key, **(step.arguments or {})}
        fingerprint = command_fingerprint(decode_command(doc, registry))
    issued_at = clock.next
    signed.append(call_id)
    return issue(
        private,
        journal_id=spec.journal_id or journal.definition.journal_id,
        approval_id=spec.approval_id,
        approver=spec.approver or sc.setup.approvals.approver,
        fingerprint=fingerprint,
        key=key,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=spec.expires_in_seconds),
    ).to_json()


def run_script(sc: Scenario, workdir: Path) -> tuple[TraceV2, list[str]]:
    """Setup then script through a fresh journal; returns the derived trace and the ids of
    the steps the runner signed artefacts for."""
    from ledgergate.derive import trace as derive_trace

    path = str(workdir / f"{sc.id}.journal")
    journal, clock = _setup_journal(sc, path)
    signed: list[str] = []
    entries: dict[str, str] = {}
    try:
        _apply(journal, clock, sc, sc.setup.before, "setup", entries, signed)
        _apply(journal, clock, sc, sc.agent.script or (), "agent", entries, signed)
    finally:
        journal.close()
    return derive_trace(path), signed


def emit_setup(sc: Scenario, path: Path) -> None:
    """The live path: create the journal with `before` applied, and the policy document."""
    policy_path = path.with_name(path.name + ".policy.json")
    if path.exists() or policy_path.exists():
        raise CorpusError(f"{path}: exists; --emit-setup refuses to overwrite")
    if sc.scripted_only:
        raise CorpusError(f"{sc.id}: scripted_only; --emit-setup refuses it")
    if sc.setup.policy is not None:
        policy_path.write_text(json.dumps(sc.setup.policy, indent=2, sort_keys=True) + "\n")
    journal, clock = _setup_journal(sc, str(path))
    try:
        _apply(journal, clock, sc, sc.setup.before, "setup", {}, [])
    finally:
        journal.close()


# ------------------------------------------------------------------ scoring


@dataclass(frozen=True)
class _Row:
    resolution: InvocationResolution
    call: ToolCallEvent
    decision: PolicyDecision | None
    command: LedgerCommandEvent | None
    result: LedgerResultEvent | None


def _rows(t: TraceV2) -> list[_Row]:
    rows: list[_Row] = []
    current: dict[str, Any] = {}
    for e in t.events:
        if isinstance(e, ToolCallEvent):
            current = {"call": e}
        elif isinstance(e, InvocationResolution):
            current["resolution"] = e
        elif isinstance(e, PolicyDecision):
            current["decision"] = e
        elif isinstance(e, LedgerCommandEvent):
            current["command"] = e
        elif isinstance(e, LedgerResultEvent):
            current["result"] = e
            if "resolution" in current:
                pass
        if e.type == "tool_result" and "resolution" in current:
            rows.append(
                _Row(
                    current["resolution"],
                    current["call"],
                    current.get("decision"),
                    current.get("command"),
                    current.get("result"),
                )
            )
            current = {}
    return rows


def _produced_outcome(row: _Row) -> str | None:
    if row.resolution.disposition not in ("new", "approval") or row.decision is None:
        return None
    if row.decision.matched_rule.startswith("runtime."):
        return None
    if row.decision.decision == "deny":
        return "denied"
    if row.decision.decision == "approval_required":
        return "awaiting_approval"
    if row.result is not None:
        return "applied" if row.result.ok else "rejected"
    return None


def behavioural_digest(t: TraceV2, agent_rows: list[_Row]) -> str:
    items: list[Any] = []
    applied_entries: list[str] = []
    setup_entries: set[str] = set()
    for row in _rows(t):
        if row.result is not None and row.result.ok and row.result.entry_id is not None:
            if row in agent_rows:
                applied_entries.append(row.result.entry_id)
            else:
                setup_entries.add(row.result.entry_id)
    for row in agent_rows:
        r = row.resolution
        content: Any
        if r.disposition == "invalid":
            content = None
        elif r.disposition == "read":
            content = canonical_text(row.call.arguments)
        elif row.command is not None and row.command.command.kind == "reverse":
            target_id = getattr(row.command.command, "entry_id", None)
            target: Any
            if target_id in applied_entries:
                target = ["agent", applied_entries.index(target_id)]
            elif target_id in setup_entries:
                target = ["setup", target_id]
            else:
                target = None
            content = ["reverse", target, getattr(row.command.command, "description", "")]
        else:
            content = r.attempted_digest
        items.append(
            [
                row.call.tool,
                r.disposition,
                _produced_outcome(row),
                row.decision.decision if row.decision else None,
                row.decision.matched_rule if row.decision else None,
                content,
                row.result.ok if row.result else None,
                row.result.sequence if row.result else None,
            ]
        )
    ledger = replay_trace(t.ledger_view()).ledger
    balances = {a: str(ledger.balance(a).amount) for a in sorted(t.chart_of_accounts())}
    return digest({"items": items, "balances": balances})


def _bind(t: TraceV2, sc: Scenario, setup_trace: TraceV2) -> str | None:
    """Spec: the trace must be from this scenario's setup."""
    if t.chart is None or [a.model_dump() for a in t.chart] != [
        a.model_dump() for a in setup_trace.chart or ()
    ]:
        return "setup mismatch: chart"
    have = {c.code: c.exponent for c in t.currencies or ()}
    for c in sc.setup.currencies:
        if have.get(c.code) != c.exponent:
            return "setup mismatch: currencies"
    if (
        t.policy_set_version != setup_trace.policy_set_version
        or t.policy_config_digest != setup_trace.policy_config_digest
    ):
        return "setup mismatch: policy"
    rows, setup_rows = _rows(t), _rows(setup_trace)
    n = len(sc.setup.before)
    if len(rows) < n or len(setup_rows) != n:
        return "setup mismatch: fewer invocations than the setup"
    for i in range(n):
        if (
            rows[i].call.call_id != f"setup-{i + 1}"
            or rows[i].resolution.attempted_digest != setup_rows[i].resolution.attempted_digest
        ):
            return f"setup mismatch: invocation {i + 1}"
    return None


def score(
    sc: Scenario, ex: Expectations, t: TraceV2, agent_rows: list[_Row], card: Scorecard
) -> list[ExpectationDoc]:
    out: list[ExpectationDoc] = []

    def add(key: str, expected: Any, actual: Any) -> None:
        out.append(
            ExpectationDoc(
                key=key,
                status="pass" if expected == actual else "fail",
                expected=expected,
                actual=actual,
            )
        )

    add("status", ex.status or "pass", card.status)
    if ex.dispositions is not None:
        actual = dict.fromkeys(ex.dispositions, 0)
        for row in agent_rows:
            actual[row.resolution.disposition] = actual.get(row.resolution.disposition, 0) + 1
        add(
            "dispositions",
            dict(ex.dispositions),
            {k: v for k, v in actual.items() if v or k in ex.dispositions},
        )
    if ex.outcomes is not None:
        actual = dict.fromkeys(ex.outcomes, 0)
        for row in agent_rows:
            o = _produced_outcome(row)
            if o is not None:
                actual[o] = actual.get(o, 0) + 1
        add("outcomes", dict(ex.outcomes), actual)
    if ex.matched_rules is not None:
        actual = dict.fromkeys(ex.matched_rules, 0)
        for row in agent_rows:
            if row.decision is not None:
                actual[row.decision.matched_rule] = actual.get(row.decision.matched_rule, 0) + 1
        add("matched_rules", dict(ex.matched_rules), actual)
    if ex.balances is not None:
        ledger = replay_trace(t.ledger_view()).ledger
        actual_b = {
            a: (str(ledger.balance(a).amount) if a in ledger.chart else None) for a in ex.balances
        }
        add("balances", dict(ex.balances), actual_b)
    if ex.ledger_commands is not None:
        add(
            "ledger_commands",
            ex.ledger_commands,
            sum(1 for r in agent_rows if r.command is not None),
        )
    if ex.invocations is not None:
        add("invocations", ex.invocations, len(agent_rows))
    return out


def _scorecard_doc(card: Scorecard) -> ScorecardDoc:
    return ScorecardDoc.model_validate(card.as_json())


def run(
    corpus: Corpus,
    *,
    traces: Path | None = None,
    only: tuple[str, ...] = (),
    kind: Kind | None = None,
    keep_traces: Path | None = None,
) -> Result:
    from ledgergate.trace import dump_v2

    unknown = sorted(set(only) - {s.id for s in corpus.scenarios})
    if unknown:
        raise CorpusError(f"--only names unknown scenarios {unknown}")
    selected = [
        s
        for s in corpus.scenarios
        if (not only or s.id in only) and (kind is None or s.kind == kind)
    ]
    results: list[ScenarioResult] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for sc in sorted(selected, key=lambda s: s.id):
            ex = corpus.expectations[sc.id]
            base = {"id": sc.id, "kind": sc.kind, "title": sc.title}
            supplied = traces / f"{sc.id}.json" if traces is not None else None
            try:
                setup_only = sc.model_copy(update={"agent": Agent(script=())})
                setup_dir = work / "setup" / sc.id
                setup_dir.mkdir(parents=True)
                setup_trace, _ = run_script(setup_only, setup_dir)
            except (JournalError, UnresolvedEntryRefError, OSError) as exc:
                results.append(
                    ScenarioResult(
                        **base,
                        status="error",
                        source="none",
                        error=f"setup failed: {type(exc).__name__}",
                    )
                )
                continue
            if supplied is not None and supplied.exists():
                source = "trace"
                try:
                    t = load_any(supplied.read_text(encoding="utf-8"))
                except (TraceError, OSError, ValueError) as exc:
                    results.append(
                        ScenarioResult(
                            **base,
                            status="error",
                            source=source,
                            error=f"unreadable trace: {type(exc).__name__}",
                        )
                    )
                    continue
            elif sc.agent.script is not None:
                source = "script"
                try:
                    script_dir = work / "script" / sc.id
                    script_dir.mkdir(parents=True)
                    t, _signed = run_script(sc, script_dir)
                except UnresolvedEntryRefError as exc:
                    results.append(
                        ScenarioResult(
                            **base,
                            status="error",
                            source=source,
                            error=f"unresolved entry_ref: {exc}",
                        )
                    )
                    continue
                except JournalError as exc:
                    results.append(
                        ScenarioResult(
                            **base,
                            status="error",
                            source=source,
                            error=f"journal refused the script: {type(exc).__name__}",
                        )
                    )
                    continue
                if keep_traces is not None:
                    keep_traces.mkdir(parents=True, exist_ok=True)
                    (keep_traces / f"{sc.id}.json").write_text(dump_v2(t), encoding="utf-8")
            else:
                results.append(ScenarioResult(**base, status="skipped", source="none"))
                continue
            problem = _bind(t, sc, setup_trace)
            if problem is not None:
                results.append(ScenarioResult(**base, status="error", source=source, error=problem))
                continue
            rows = _rows(t)
            agent_rows = rows[len(sc.setup.before) :]
            card = check(t)
            expectations = score(sc, ex, t, agent_rows, card)
            status: Literal["pass", "fail"] = (
                "pass" if all(e.status == "pass" for e in expectations) else "fail"
            )
            results.append(
                ScenarioResult(
                    **base,
                    status=status,
                    source=source,
                    trace_digest=behavioural_digest(t, agent_rows),
                    scorecard=_scorecard_doc(card),
                    expectations=tuple(expectations),
                )
            )
    return Result(
        ledgergate_version=__version__,
        corpus_digest=corpus.digest,
        summary=summarize(results),
        selection=Selection(only=tuple(sorted(only)), kind=kind),
        scenarios=tuple(results),
    )


__all__ = [
    "Corpus",
    "CorpusError",
    "Expectations",
    "PeekClock",
    "Scenario",
    "Step",
    "behavioural_digest",
    "emit_setup",
    "load_corpus",
    "run",
    "run_script",
]
