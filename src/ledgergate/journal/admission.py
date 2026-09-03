# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Admission: turning one untyped JSON value into a canonical ``Request``.

The transport hands admission a decoded I-JSON value (see :mod:`ledgergate.codec.ijson`).
Admission's output on success is a :class:`Request`. On failure it raises
:class:`AdmissionError` with a code and the failing field path and *no values*, because
the failure envelope the journal writes must not carry the input it could not decode.

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
from ledgergate.ledger import Command, Currency, InvalidIdentifierError
from ledgergate.ledger.identifiers import require_identifier

WRITE_TOOLS = frozenset({"post", "reverse", "open_transaction", "advance", "refund"})
READ_TOOLS = frozenset({"balance", "trial_balance"})
TOOLS = WRITE_TOOLS | READ_TOOLS


class AdmissionError(Exception):
    """The input is not an admissible request. Carries no input values."""

    def __init__(self, code: str, path: str = "") -> None:
        self.code, self.path = code, path
        super().__init__(f"{code} at {path or '$'}")


@dataclass(frozen=True, slots=True)
class Request:
    """What admission produces. ``arguments`` is the admitted (post-redaction) document."""

    tool: str
    arguments: dict[str, Any]
    call_id: str
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
        }
        if self.key is not None:
            body["key"] = self.key
        if self.approval is not None:
            body["approval"] = self.approval
        return digest(body)


class Admitter(Protocol):
    def admit(self, value: Any, registry: dict[str, Currency]) -> Request: ...


def _str_field(obj: dict[str, Any], name: str, *, required: bool = True) -> str | None:
    if name not in obj:
        if required:
            raise AdmissionError("missing_field", name)
        return None
    v = obj[name]
    if not isinstance(v, str):
        raise AdmissionError("wrong_type", name)
    return v


def _identifier(value: str, path: str) -> str:
    try:
        return require_identifier(value, path)
    except InvalidIdentifierError as exc:
        raise AdmissionError("invalid_identifier", path) from exc


class IdentityAdmitter:
    """Validate shape and identifiers; change nothing. Refuses approval artefacts."""

    def admit(self, value: Any, registry: dict[str, Currency]) -> Request:
        if not isinstance(value, dict):
            raise AdmissionError("not_an_object")
        if extra := set(value) - {"tool", "arguments", "call_id", "key", "approval"}:
            raise AdmissionError("unknown_field", sorted(extra)[0])
        tool = _str_field(value, "tool")
        assert tool is not None
        if tool not in TOOLS:
            raise AdmissionError("unknown_tool", "tool")
        call_id = _identifier(_str_field(value, "call_id") or "", "call_id")
        arguments = value.get("arguments", {})
        if not isinstance(arguments, dict):
            raise AdmissionError("wrong_type", "arguments")
        if "approval" in value and value["approval"] is not None:
            raise AdmissionError("approval_unsupported", "approval")

        if tool in READ_TOOLS:
            if "key" in value:
                raise AdmissionError("unexpected_field", "key")
            return Request(tool, arguments, call_id, None)

        key = _identifier(_str_field(value, "key") or "", "key")
        if "kind" in arguments or "key" in arguments:
            raise AdmissionError("unexpected_field", "arguments.kind")
        doc = {"kind": tool, "key": key, **arguments}
        try:
            command = decode_command(doc, registry)
        except CodecError as exc:
            raise AdmissionError("malformed_command", _codec_path(str(exc))) from exc
        return Request(tool, arguments, call_id, key, None, command)


def _codec_path(message: str) -> str:
    # CodecError messages are "<where>: <detail>"; keep only the location.
    return message.split(":", 1)[0].strip()
