# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The one JSON form of a ledger command.

The shape is exactly the ``command`` object of trace schema v1, so a journal row and a
trace event carry the same bytes for the same command. ``ledgergate.trace.models``
delegates to these functions; ``ledgergate.journal`` stores their output. The invariant
this module is tested to is ``command_fingerprint(decode(encode(c))) == command_fingerprint(c)``
for every command the core accepts.

Decoding is *structural*: it builds the runtime objects and lets the core's own
constructors raise their own errors (an unbalanced draft, a zero posting). It does not
second-guess them. A currency code that is not in the supplied registry is a
:class:`CodecError`, because no exponent means no amount.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ledgergate.ledger import (
    Advance,
    Command,
    Currency,
    EntryDraft,
    Money,
    OpenTransaction,
    Post,
    Posting,
    Refund,
    Reverse,
    Side,
    TransactionEvent,
)

CODEC_VERSION = "1"

Registry = Mapping[str, Currency]


class CodecError(ValueError):
    """The document is not a well-formed command in this codec version.

    ``where`` is the structural location (a literal field path built by this module,
    never from document values); ``detail`` says what was wrong there.
    """

    def __init__(self, where: str, detail: str) -> None:
        self.where, self.detail = where, detail
        super().__init__(f"{where}: {detail}")


# ------------------------------------------------------------------- encoding


def _money(money: Money) -> dict[str, Any]:
    return {"amount": money.amount, "currency": money.currency.code}


def encode_draft(draft: EntryDraft) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "postings": [
            {"account": p.account_id, "side": p.side.value, "money": _money(p.money)}
            for p in draft.postings
        ]
    }
    if draft.description:
        doc["description"] = draft.description
    if draft.tags:
        doc["tags"] = dict(draft.tags)
    return doc


def encode_command(command: Command) -> dict[str, Any]:
    match command:
        case Post(key, draft):
            return {"kind": "post", "key": key, "draft": encode_draft(draft)}
        case Reverse(key, entry_id, description):
            doc: dict[str, Any] = {"kind": "reverse", "key": key, "entry_id": entry_id}
            if description:
                doc["description"] = description
            return doc
        case OpenTransaction(key, transaction_id, amount):
            return {
                "kind": "open_transaction",
                "key": key,
                "transaction_id": transaction_id,
                "amount": _money(amount),
            }
        case Advance(key, transaction_id, event, entry):
            doc = {
                "kind": "advance",
                "key": key,
                "transaction_id": transaction_id,
                "event": event.value,
            }
            if entry is not None:
                doc["entry"] = encode_draft(entry)
            return doc
        case Refund(key, transaction_id, money, entry):
            doc = {
                "kind": "refund",
                "key": key,
                "transaction_id": transaction_id,
                "money": _money(money),
            }
            if entry is not None:
                doc["entry"] = encode_draft(entry)
            return doc


# ------------------------------------------------------------------- decoding


def _expect(doc: Any, kind: str) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise CodecError(kind, "must be an object")
    return doc


def _field(doc: dict[str, Any], name: str, typ: type, *, where: str) -> Any:
    if name not in doc:
        raise CodecError(where, f"missing {name!r}")
    value = doc[name]
    if (typ is int and isinstance(value, bool)) or not isinstance(value, typ):
        raise CodecError(f"{where}.{name}", f"expected {typ.__name__}, got {type(value).__name__}")
    return value


def _only(doc: dict[str, Any], allowed: set[str], *, where: str) -> None:
    if extra := set(doc) - allowed:
        raise CodecError(where, f"unknown fields {sorted(extra)}")


def _decode_money(doc: Any, registry: Registry, *, where: str) -> Money:
    m = _expect(doc, where)
    _only(m, {"amount", "currency"}, where=where)
    amount = _field(m, "amount", int, where=where)
    code = _field(m, "currency", str, where=where)
    if code not in registry:
        raise CodecError(where, f"currency {code!r} is not in the registry")
    return Money(amount, registry[code])


MAX_POSTINGS = 1000
"""The trace schema's bound on an entry's postings; admission refuses beyond it so every
admitted command is representable in a trace."""


def decode_draft(doc: Any, registry: Registry, *, where: str = "draft") -> EntryDraft:
    d = _expect(doc, where)
    _only(d, {"postings", "description", "tags"}, where=where)
    raw = _field(d, "postings", list, where=where)
    if len(raw) > MAX_POSTINGS:
        raise CodecError(f"{where}.postings", f"more than {MAX_POSTINGS} postings")
    postings = []
    for i, p in enumerate(raw):
        pw = f"{where}.postings[{i}]"
        pd = _expect(p, pw)
        _only(pd, {"account", "side", "money"}, where=pw)
        side_raw = _field(pd, "side", str, where=pw)
        try:
            side = Side(side_raw)
        except ValueError as exc:
            raise CodecError(f"{pw}.side", f"{side_raw!r}") from exc
        postings.append(
            Posting(
                _field(pd, "account", str, where=pw),
                side,
                _decode_money(pd.get("money"), registry, where=f"{pw}.money"),
            )
        )
    description = d.get("description", "")
    if not isinstance(description, str):
        raise CodecError(f"{where}.description", "expected str")
    tags_raw = d.get("tags", {})
    if not isinstance(tags_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in tags_raw.items()
    ):
        raise CodecError(f"{where}.tags", "expected an object of strings")
    return EntryDraft(tuple(postings), description, tuple(sorted(tags_raw.items())))


def decode_command(doc: Any, registry: Registry) -> Command:
    c = _expect(doc, "command")
    kind = _field(c, "kind", str, where="command")
    key = _field(c, "key", str, where="command")
    where = f"command({kind})"
    match kind:
        case "post":
            _only(c, {"kind", "key", "draft"}, where=where)
            return Post(key, decode_draft(c.get("draft"), registry, where=f"{where}.draft"))
        case "reverse":
            _only(c, {"kind", "key", "entry_id", "description"}, where=where)
            description = c.get("description", "")
            if not isinstance(description, str):
                raise CodecError(f"{where}.description", "expected str")
            return Reverse(key, _field(c, "entry_id", str, where=where), description)
        case "open_transaction":
            _only(c, {"kind", "key", "transaction_id", "amount"}, where=where)
            return OpenTransaction(
                key,
                _field(c, "transaction_id", str, where=where),
                _decode_money(c.get("amount"), registry, where=f"{where}.amount"),
            )
        case "advance":
            _only(c, {"kind", "key", "transaction_id", "event", "entry"}, where=where)
            event_raw = _field(c, "event", str, where=where)
            try:
                event = TransactionEvent(event_raw)
            except ValueError as exc:
                raise CodecError(f"{where}.event", f"{event_raw!r}") from exc
            entry = (
                None
                if "entry" not in c
                else decode_draft(c["entry"], registry, where=f"{where}.entry")
            )
            return Advance(key, _field(c, "transaction_id", str, where=where), event, entry)
        case "refund":
            _only(c, {"kind", "key", "transaction_id", "money", "entry"}, where=where)
            entry = (
                None
                if "entry" not in c
                else decode_draft(c["entry"], registry, where=f"{where}.entry")
            )
            return Refund(
                key,
                _field(c, "transaction_id", str, where=where),
                _decode_money(c.get("money"), registry, where=f"{where}.money"),
                entry,
            )
        case _:
            raise CodecError("command", f"unknown kind {kind!r}")
