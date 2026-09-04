# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The seven tools and their input schemas, generated from one source.

Every write schema carries the two reserved top-level members the server lifts out before
the journal sees the arguments: ``idempotency_key`` (required) and ``approval`` (optional).
Read schemas carry neither. The schema is advisory to the client; admission does not trust it.
"""

from __future__ import annotations

from typing import Any

from ledgergate.codec import MAX_POSTINGS, MAX_TAGS, MAX_TEXT
from ledgergate.journal.admission import READ_TOOLS, WRITE_TOOLS

IDEMPOTENCY_KEY = "idempotency_key"
APPROVAL = "approval"

_IDENTIFIER: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": 256}
_MONEY: dict[str, Any] = {
    "type": "object",
    "properties": {
        "amount": {"type": "integer", "description": "minor units"},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
    },
    "required": ["amount", "currency"],
    "additionalProperties": False,
}
_POSTING: dict[str, Any] = {
    "type": "object",
    "properties": {
        "account": _IDENTIFIER,
        "side": {"type": "string", "enum": ["debit", "credit"]},
        "money": _MONEY,
    },
    "required": ["account", "side", "money"],
    "additionalProperties": False,
}
_DRAFT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "postings": {"type": "array", "items": _POSTING, "minItems": 2, "maxItems": MAX_POSTINGS},
        "description": {"type": "string", "maxLength": MAX_TEXT},
        "tags": {
            "type": "object",
            "additionalProperties": {"type": "string", "maxLength": MAX_TEXT},
            "maxProperties": MAX_TAGS,
        },
    },
    "required": ["postings"],
    "additionalProperties": False,
}
_APPROVAL: dict[str, Any] = {
    "type": "object",
    "description": "an Ed25519 approval artefact issued by `ledgergate approve`",
}

_ARGUMENTS: dict[str, dict[str, Any]] = {
    "post": {"draft": _DRAFT},
    "reverse": {"entry_id": _IDENTIFIER, "description": {"type": "string", "maxLength": MAX_TEXT}},
    "open_transaction": {"transaction_id": _IDENTIFIER, "amount": _MONEY},
    "advance": {
        "transaction_id": _IDENTIFIER,
        "event": {
            "type": "string",
            "enum": ["authorize", "settle", "dispute", "resolve_dispute", "cancel", "fail"],
        },
        "entry": _DRAFT,
    },
    "refund": {"transaction_id": _IDENTIFIER, "money": _MONEY, "entry": _DRAFT},
    "balance": {"account": _IDENTIFIER},
    "trial_balance": {},
}
_REQUIRED: dict[str, list[str]] = {
    "post": ["draft"],
    "reverse": ["entry_id"],
    "open_transaction": ["transaction_id", "amount"],
    "advance": ["transaction_id", "event"],
    "refund": ["transaction_id", "money"],
    "balance": ["account"],
    "trial_balance": [],
}
_DESCRIPTIONS = {
    "post": "Post a balanced journal entry.",
    "reverse": "Reverse a posted entry by id.",
    "open_transaction": "Open a payment transaction for an amount.",
    "advance": "Advance a transaction's lifecycle; settle and refund events carry an entry.",
    "refund": "Refund part or all of a settled transaction, with the entry that moves it.",
    "balance": "The balance of one account, with the journal cursor it was read at.",
    "trial_balance": "Every account's debits and credits, with the journal cursor.",
}


def _schema(tool: str) -> dict[str, Any]:
    props = dict(_ARGUMENTS[tool])
    required = list(_REQUIRED[tool])
    if tool in WRITE_TOOLS:
        props[IDEMPOTENCY_KEY] = {
            **_IDENTIFIER,
            "description": "the journal's idempotency key: a retry with the same key replays",
        }
        props[APPROVAL] = _APPROVAL
        required.append(IDEMPOTENCY_KEY)
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    tool: _schema(tool) for tool in sorted(WRITE_TOOLS | READ_TOOLS)
}


def tool_list() -> list[dict[str, Any]]:
    return [
        {"name": tool, "description": _DESCRIPTIONS[tool], "inputSchema": TOOL_SCHEMAS[tool]}
        for tool in TOOL_SCHEMAS
    ]
