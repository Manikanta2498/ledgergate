# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Admission: turning one untyped JSON value into a canonical ``Request``.

The transport hands admission a decoded I-JSON value (see :mod:`ledgergate.codec.ijson`).
Admission's output on success is a :class:`Request`. On failure it raises
:class:`AdmissionError` with a code and the failing field path and *no values*, because
the failure envelope the journal writes must not carry the input it could not decode.

Every way a request can be unusable is an admission failure, so that it is recorded: an
unknown tool, a malformed shape, an identifier the core would refuse, a command document
the codec cannot decode, and a command the core's own constructors reject (an unbalanced
draft, a zero posting). Nothing caller-controlled escapes the journal unrecorded.

M2b shipped the :class:`IdentityAdmitter`: identifiers are validated by the core's
``require_identifier`` and passed through, free text is passed through. M2c added the
tokenizing, redacting one behind the same :class:`Admitter` protocol. From M3 both accept an
approval artefact, validated for *shape* here (the checks that decide its verdict are the
journal's, inside the transaction); its ``key`` is already the stored token, since the
approver issued it against what the journal holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ledgergate.codec import CodecError, Tokenizer, decode_command, digest
from ledgergate.journal.approvals import Approval, ApprovalError
from ledgergate.ledger import (
    Advance,
    ChartOfAccounts,
    Command,
    Currency,
    InvalidIdentifierError,
    Ledger,
    LedgerError,
    OpenTransaction,
    Post,
    Refund,
    Reverse,
)
from ledgergate.ledger.identifiers import require_identifier

WRITE_TOOLS = frozenset({"post", "reverse", "open_transaction", "advance", "refund"})
READ_TOOLS = frozenset({"balance", "trial_balance"})
TOOLS = WRITE_TOOLS | READ_TOOLS


class AdmissionError(Exception):
    """The input is not an admissible request. Carries a code and a path, never a value."""

    def __init__(self, code: str, path: str = "") -> None:
        self.code, self.path = code, path
        super().__init__(f"{code} at {path or '$'}")


@dataclass(frozen=True, slots=True)
class Request:
    """What admission produces. ``arguments`` is the admitted (post-redaction) document."""

    tool: str
    arguments: dict[str, Any]
    call_id: str
    principal: str
    key: str | None  # None for read tools
    approval: dict[str, Any] | None = None
    command: Command | None = field(default=None, compare=False)  # decoded, write tools only

    @property
    def is_read(self) -> bool:
        return self.tool in READ_TOOLS

    def request_digest(self) -> str:
        """SHA-256 over the canonical serialization of the admitted request."""
        body: dict[str, Any] = {
            "tool": self.tool,
            "arguments": self.arguments,
            "call_id": self.call_id,
            "principal": self.principal,
        }
        if self.key is not None:
            body["key"] = self.key
        if self.approval is not None:
            body["approval"] = self.approval
        return digest(body)


@dataclass(frozen=True, slots=True)
class AdmissionScope:
    """What an admitter may consult: the definition's registry and chart, who asks, and
    the current projection (for resolving references to runtime-generated ids)."""

    registry: dict[str, Currency]
    chart: ChartOfAccounts
    principal: str
    ledger: Ledger


class Admitter(Protocol):
    """The admission seam. M2b's identity implementation changes nothing; M2c's tokenizes
    and redacts. ``token_domain`` and ``token_key_version`` are recorded in the definition
    at creation and checked at open, so a journal is never read with a different key."""

    token_domain: str
    token_key_version: str

    def key_check(self) -> str:
        """Identifies the key without revealing it; stored at creation, compared at open.
        ``none`` for the identity admitter."""
        ...

    def admit(self, value: Any, scope: AdmissionScope) -> Request: ...

    def redact_text(self, text: str) -> str:
        """Free text the admitter does not see through ``admit``: standalone message
        content, account names in the definition, and the failure envelope's bounded
        payload. The core's own error messages are *not* redacted: the core only ever sees
        the admitted command, so they carry tokens and operator identifiers, and a derived
        trace must replay them byte for byte."""
        ...

    def tokenize_identifier(self, value: str) -> str:
        """A caller identifier recovered from a request ``admit`` rejected (the envelope's
        ``call_id``). Identity in M2b; keyed tokenization in M2c."""
        ...

    def digest_input(self, value: Any) -> str:
        """The envelope's ``input_digest`` over the raw, pre-admission input. Plain SHA-256
        over JCS in M2b; keyed under the token key in M2c, so a stored digest of rejected
        content is not a dictionary-reversible commitment to it."""
        ...


def _str_field(obj: dict[str, Any], name: str) -> str:
    if name not in obj:
        raise AdmissionError("missing_field", name)
    v = obj[name]
    if not isinstance(v, str):
        raise AdmissionError("wrong_type", name)
    return v


def _identifier(value: str, path: str) -> str:
    try:
        return require_identifier(value, path)
    except InvalidIdentifierError as exc:
        raise AdmissionError("invalid_identifier", path) from exc


def _read_arguments(tool: str, arguments: dict[str, Any], chart: ChartOfAccounts) -> None:
    if tool == "balance":
        if set(arguments) != {"account"}:
            raise AdmissionError("wrong_shape", "arguments")
        account = arguments["account"]
        if not isinstance(account, str):
            raise AdmissionError("wrong_type", "arguments.account")
        if account not in set(chart):
            raise AdmissionError("unknown_account", "arguments.account")
    elif arguments:
        raise AdmissionError("wrong_shape", "arguments")


class IdentityAdmitter:
    """Validate shape and identifiers; change nothing. Refuses approval artefacts.

    Also the structural base of :class:`TokenizingAdmitter`, which overrides only the four
    transforms; the shape rules, error codes and paths are identical in both.
    """

    token_domain = "none"  # noqa: S105 - a label, not a credential
    token_key_version = "none"  # noqa: S105 - a label, not a credential

    def key_check(self) -> str:
        return "none"

    def redact_text(self, text: str) -> str:
        return text

    def tokenize_identifier(self, value: str) -> str:
        return value

    def digest_input(self, value: Any) -> str:
        return digest(value)

    def _arguments(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The admitted ``arguments`` for a write tool. Identity here."""
        del tool
        return arguments

    def admit(self, value: Any, scope: AdmissionScope) -> Request:
        if not isinstance(value, dict):
            raise AdmissionError("not_an_object")
        if set(value) - {"tool", "arguments", "call_id", "key", "approval"}:
            # The offending member name is caller-controlled and unbounded; it is not
            # repeated in the path. It is recoverable from the envelope payload, which
            # passes through the redactor and the byte bound.
            raise AdmissionError("unknown_field", "$")
        tool = _str_field(value, "tool")
        if tool not in TOOLS:
            raise AdmissionError("unknown_tool", "tool")
        call_id = self.tokenize_identifier(_identifier(_str_field(value, "call_id"), "call_id"))
        arguments = value.get("arguments", {})
        if not isinstance(arguments, dict):
            raise AdmissionError("wrong_type", "arguments")
        approval = value.get("approval")
        if approval is not None:
            try:
                approval = Approval.from_json(approval).to_json()
            except ApprovalError as exc:
                raise AdmissionError("approval_malformed", "approval") from exc

        if tool in READ_TOOLS:
            if "key" in value:
                raise AdmissionError("unexpected_field", "key")
            _read_arguments(tool, arguments, scope.chart)
            return Request(tool, arguments, call_id, scope.principal, None, approval)

        key = self.tokenize_identifier(_identifier(_str_field(value, "key"), "key"))
        for reserved in ("kind", "key"):
            if reserved in arguments:
                raise AdmissionError("unexpected_field", f"arguments.{reserved}")
        try:
            arguments = self._arguments(tool, arguments)
        except InvalidIdentifierError as exc:
            raise AdmissionError("invalid_identifier", "arguments.transaction_id") from exc
        doc = {"kind": tool, "key": key, **arguments}
        try:
            command = decode_command(doc, scope.registry)
        except CodecError as exc:
            raise AdmissionError("malformed_command", _argument_path(exc.where)) from exc
        except LedgerError as exc:
            # The codec is structural and lets the core's constructors raise. An unbalanced
            # draft is still malformed input; it is recorded as such, not lost.
            raise AdmissionError(f"malformed_command:{type(exc).__name__}", "arguments") from exc
        for path, value_ in _identifier_fields(command):
            _identifier(value_, path)
        for path, account in _account_references(command):
            if account not in set(scope.chart):
                raise AdmissionError("unknown_account", path)
        if isinstance(command, Reverse) and not scope.ledger.has_entry(command.entry_id):
            # A reference to a runtime-generated id is only free of caller content once
            # it resolves; until then it is arbitrary text and must not reach a row.
            raise AdmissionError("unknown_entry", "arguments.entry_id")
        return Request(tool, arguments, call_id, scope.principal, key, approval, command)


def _identifier_fields(command: Command) -> list[tuple[str, str]]:
    """Identifier-shaped fields a command carries besides its key, validated at admission so
    an invalid one is recorded as `invalid` rather than reaching the core as a rejection.
    ``transaction_id`` is caller-supplied (class 2: tokenized in M2c); ``entry_id`` is a
    reference to a runtime-generated id (validated, never tokenized)."""
    match command:
        case Reverse(_, entry_id, _):
            return [("arguments.entry_id", entry_id)]
        case OpenTransaction(_, transaction_id, _) | Advance(_, transaction_id, _, _):
            return [("arguments.transaction_id", transaction_id)]
        case Refund(_, transaction_id, _, _):
            return [("arguments.transaction_id", transaction_id)]
    return []


def _account_references(command: Command) -> list[tuple[str, str]]:
    """Every account a command's postings name, with its path. Chart membership is static
    configuration, so it is admission's check on writes as it is on reads."""
    match command:
        case Post(_, draft):
            return [
                (f"arguments.draft.postings[{i}].account", p.account_id)
                for i, p in enumerate(draft.postings)
            ]
        case Advance(_, _, _, entry) | Refund(_, _, _, entry) if entry is not None:
            return [
                (f"arguments.entry.postings[{i}].account", p.account_id)
                for i, p in enumerate(entry.postings)
            ]
    return []


def _argument_path(where: str) -> str:
    """Codec locations are rooted at ``command(<kind>)``; the request rooted them under
    ``arguments``. One grammar for the envelope."""
    return re.sub(r"^command(\([a-z_]+\))?", "arguments", where)


class TokenizingAdmitter(IdentityAdmitter):
    """M2c: the same shape rules, with every caller identifier tokenized and every free-text
    field redacted before the command is decoded, fingerprinted, or written anywhere.

    The stored ``arguments`` is the admitted (transformed) document, so the request digest,
    the fingerprint and every row are over the stored form, and a redacted journal replays
    exactly. Account references, amounts, currencies and sides are untouched: they are the
    books. The key never leaves the :class:`~ledgergate.codec.Tokenizer`.
    """

    def __init__(self, tokenizer: Tokenizer) -> None:
        self._tokenizer = tokenizer
        self.token_domain = tokenizer.domain
        self.token_key_version = tokenizer.key_version

    def key_check(self) -> str:
        return self._tokenizer.key_check()

    def redact_text(self, text: str) -> str:
        return self._tokenizer.redact(text)

    def tokenize_identifier(self, value: str) -> str:
        return self._tokenizer.tokenize(value)

    def digest_input(self, value: Any) -> str:
        return self._tokenizer.digest_input(value)

    def _arguments(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._tokenizer.arguments(tool, arguments)
