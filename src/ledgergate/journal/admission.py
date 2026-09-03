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

M2b ships the :class:`IdentityAdmitter`: identifiers are validated by the core's
``require_identifier`` and passed through, free text is passed through, and an approval
artefact is refused with ``approval_unsupported`` because the artefact format is an M3
deliverable. M2c replaces it, behind the same :class:`Admitter` protocol, with the
tokenizing and redacting one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ledgergate.codec import CodecError, decode_command, digest
from ledgergate.ledger import (
    ChartOfAccounts,
    Command,
    Currency,
    InvalidIdentifierError,
    LedgerError,
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
    """What an admitter may consult: the definition's registry and chart, and who asks."""

    registry: dict[str, Currency]
    chart: ChartOfAccounts
    principal: str


class Admitter(Protocol):
    """The admission seam. M2b's identity implementation changes nothing; M2c's tokenizes
    and redacts. ``token_domain`` and ``token_key_version`` are recorded in the definition
    at creation and checked at open, so a journal is never read with a different key."""

    token_domain: str
    token_key_version: str

    def admit(self, value: Any, scope: AdmissionScope) -> Request: ...

    def redact_text(self, text: str) -> str:
        """Free text the admitter does not see through ``admit``: the core's own error
        messages (which can echo a caller identifier), standalone message content, account
        names in the definition, and the failure envelope's bounded payload."""
        ...

    def tokenize_identifier(self, value: str) -> str:
        """A caller identifier recovered from a request ``admit`` rejected (the envelope's
        ``call_id``). Identity in M2b; keyed tokenization in M2c."""
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
    """Validate shape and identifiers; change nothing. Refuses approval artefacts."""

    token_domain = "none"  # noqa: S105 - a label, not a credential
    token_key_version = "none"  # noqa: S105 - a label, not a credential

    def redact_text(self, text: str) -> str:
        return text

    def tokenize_identifier(self, value: str) -> str:
        return value

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
        call_id = _identifier(_str_field(value, "call_id"), "call_id")
        arguments = value.get("arguments", {})
        if not isinstance(arguments, dict):
            raise AdmissionError("wrong_type", "arguments")
        if value.get("approval") is not None:
            raise AdmissionError("approval_unsupported", "approval")

        if tool in READ_TOOLS:
            if "key" in value:
                raise AdmissionError("unexpected_field", "key")
            _read_arguments(tool, arguments, scope.chart)
            return Request(tool, arguments, call_id, scope.principal, None)

        key = _identifier(_str_field(value, "key"), "key")
        for reserved in ("kind", "key"):
            if reserved in arguments:
                raise AdmissionError("unexpected_field", f"arguments.{reserved}")
        doc = {"kind": tool, "key": key, **arguments}
        try:
            command = decode_command(doc, scope.registry)
        except CodecError as exc:
            raise AdmissionError("malformed_command", _codec_path(str(exc))) from exc
        except LedgerError as exc:
            # The codec is structural and lets the core's constructors raise. An unbalanced
            # draft is still malformed input; it is recorded as such, not lost.
            raise AdmissionError(f"malformed_command:{type(exc).__name__}", "arguments") from exc
        return Request(tool, arguments, call_id, scope.principal, key, None, command)


def _codec_path(message: str) -> str:
    # CodecError messages are "<where>: <detail>"; keep only the location.
    return message.split(":", 1)[0].strip()
