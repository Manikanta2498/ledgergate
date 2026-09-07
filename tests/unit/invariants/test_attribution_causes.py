"""`attributions_are_registered`, the schema-7 cause/authentication matrix (docs/spec/
principals.md, *Attribution of every invocation*; docs/spec/trace-v2.md, the row's sentence).

An `invalid` row's authentication is the one its cause was reached under. A genuine
`local / rejected / bad_signature` row relabelled `agent / signed / bad_signature` was a PASS
before this rule: the forged row named a live signed principal, so every liveness check it
had was satisfied, and nothing tied the cause to the kind of refusal it names.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from ledgergate.derive import trace as derive
from ledgergate.invariants import check
from ledgergate.journal import Journal, generate_signing_key, verification_key_text
from ledgergate.journal.auth import sign_request
from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    SequentialIds,
    SteppingClock,
)
from ledgergate.trace import TraceV2

CHART = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)
AGENT = generate_signing_key()
OTHER = generate_signing_key()


def _post(key: str, call_id: str, amount: int = 5) -> dict[str, Any]:
    return {
        "tool": "post",
        "call_id": call_id,
        "key": key,
        "arguments": {
            "draft": {
                "postings": [
                    {
                        "account": "cash",
                        "side": "debit",
                        "money": {"amount": amount, "currency": "USD"},
                    },
                    {
                        "account": "revenue",
                        "side": "credit",
                        "money": {"amount": amount, "currency": "USD"},
                    },
                ]
            }
        },
    }


@pytest.fixture
def rejected(tmp_path: Path) -> dict[str, Any]:
    """A journal whose one invocation is a clockless envelope refusal: `bad_signature`,
    attributed to the session's transport principal as `rejected`."""
    path = str(tmp_path / "b.journal")
    j = Journal.create(path, CHART, clock=SteppingClock(EPOCH), ids=SequentialIds())
    j.add_principal("agent", "signed", verification_key_text(AGENT))
    value = _post("k1", "c1")
    envelope = sign_request(
        value,
        private=OTHER,  # a key no registry entry holds: the signature does not verify
        journal_id=j.definition.journal_id,
        principal="agent",
        expires_at=EPOCH + timedelta(seconds=300),
    )
    result = j.handle({**value, "auth": envelope})
    assert (result.disposition, result.error_type) == ("invalid", "bad_signature")
    j.close()
    t = derive(path)
    assert check(t).passed
    return t.model_dump(mode="json", exclude_none=True)


def _statuses(doc: dict[str, Any]) -> dict[str, str]:
    return {r.name: r.status for r in check(TraceV2.model_validate(doc)).results}


def test_a_clockless_refusal_relabelled_as_a_signed_row_is_forged(rejected: dict[str, Any]) -> None:
    doc = copy.deepcopy(rejected)
    r = next(e for e in doc["events"] if e["type"] == "invocation_resolution")
    assert (r["principal"], r["authentication"], r["error_type"]) == (
        "local",
        "rejected",
        "bad_signature",
    )
    r["principal"], r["authentication"] = "agent", "signed"
    assert _statuses(doc)["attributions_are_registered"] == "fail"


@pytest.mark.parametrize(
    ("cause", "authentication"),
    [
        ("bad_signature", "signed"),
        ("bad_signature", "transport"),
        ("unknown_principal", "signed"),
        ("authentication_malformed", "transport"),
        ("request_expired", "rejected"),
        ("request_expiry_unbounded", "transport"),
        ("replayed_call", "rejected"),
        ("revoked_principal", "signed"),
        ("unknown_tool", "rejected"),
        ("payload_too_large", "rejected"),
    ],
)
def test_every_contradiction_of_the_matrix_fails(
    rejected: dict[str, Any], cause: str, authentication: str
) -> None:
    doc = copy.deepcopy(rejected)
    r = next(e for e in doc["events"] if e["type"] == "invocation_resolution")
    r["error_type"] = cause
    r["authentication"] = authentication
    if authentication != "rejected":
        # a non-rejected row is attributed to the name that authenticated, and `agent` is the
        # only signed entry; `transport` keeps the session's `local`
        r["principal"] = "agent" if authentication == "signed" else "local"
    tr = next(e for e in doc["events"] if e["type"] == "tool_result")
    tr["error"]["type"] = cause  # the model requires error_type to be the served error
    assert _statuses(doc)["attributions_are_registered"] == "fail"


@pytest.mark.parametrize(
    ("cause", "authentication", "principal"),
    [
        ("bad_signature", "rejected", "local"),
        ("unknown_principal", "rejected", "local"),
        ("authentication_malformed", "rejected", "local"),
        ("unknown_tool", "transport", "local"),
        ("unknown_tool", "signed", "agent"),
    ],
)
def test_the_combinations_the_matrix_admits_still_pass(
    rejected: dict[str, Any], cause: str, authentication: str, principal: str
) -> None:
    doc = copy.deepcopy(rejected)
    r = next(e for e in doc["events"] if e["type"] == "invocation_resolution")
    r["error_type"], r["authentication"], r["principal"] = cause, authentication, principal
    tr = next(e for e in doc["events"] if e["type"] == "tool_result")
    tr["error"]["type"] = cause
    assert _statuses(doc)["attributions_are_registered"] == "pass"
