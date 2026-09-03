# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Approval artefacts, per ``docs/spec/journal.md`` *Approval artefacts*.

An artefact is issued out of band by an approver holding an Ed25519 signing key; the
journal's definition holds the verification key. It binds to exactly one pending operation
in exactly one journal (``journal_id``, ``fingerprint``, tokenized ``key``) and carries
display fields the approver saw, signed but never compared. The signature covers every
field, serialized per RFC 8785, so nothing can be re-labelled after issuance.

The pure checks (signature, expiry, scope) live here; consumption is the journal's, since
it is a row.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ledgergate.codec import canonical_bytes
from ledgergate.ledger import InvalidIdentifierError
from ledgergate.ledger.identifiers import require_identifier

CheckResult = Literal[
    "checks_passed",
    "approval_invalid",
    "approval_expired",
    "approval_scope_mismatch",
    "approval_not_applicable",
]
Verdict = Literal[
    "approval_valid",
    "approval_already_used",
    "approval_not_applicable",
    "approval_invalid",
    "approval_expired",
    "approval_scope_mismatch",
]

_GRAMMAR = {
    "journal_id": re.compile(r"[0-9a-f]{32}"),
    "fingerprint": re.compile(r"[0-9a-f]{64}"),
    "signature": re.compile(r"[A-Za-z0-9_-]{86}"),
    "amount": re.compile(r"-?[0-9]{1,40}"),
    "currency": re.compile(r"[A-Z]{3}"),
}

SIGNED_FIELDS = (
    "journal_id",
    "approval_id",
    "approver",
    "fingerprint",
    "key",
    "subject",
    "amount",
    "currency",
    "issued_at",
    "expires_at",
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ApprovalError(ValueError):
    """The artefact is not well-formed (shape, not verdict)."""


@dataclass(frozen=True, slots=True)
class Approval:
    """A parsed artefact. ``signature`` is base64url over the JCS of :data:`SIGNED_FIELDS`."""

    journal_id: str
    approval_id: str
    approver: str
    fingerprint: str
    key: str
    subject: str | None
    amount: str | None
    currency: str | None
    issued_at: datetime
    expires_at: datetime
    signature: str

    def signed_payload(self) -> dict[str, Any]:
        return {
            "journal_id": self.journal_id,
            "approval_id": self.approval_id,
            "approver": self.approver,
            "fingerprint": self.fingerprint,
            "key": self.key,
            "subject": self.subject,
            "amount": self.amount,
            "currency": self.currency,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    def to_json(self) -> dict[str, Any]:
        return {**self.signed_payload(), "signature": self.signature}

    @classmethod
    def from_json(cls, doc: Any) -> Approval:
        if not isinstance(doc, dict):
            raise ApprovalError("approval must be an object")
        if set(doc) != {*SIGNED_FIELDS, "signature"}:
            raise ApprovalError("approval has missing or unknown fields")
        for name in ("journal_id", "approval_id", "approver", "fingerprint", "key", "signature"):
            if not isinstance(doc[name], str):
                raise ApprovalError(f"approval.{name} must be a string")
        for name in ("subject", "amount", "currency"):
            if doc[name] is not None and not isinstance(doc[name], str):
                raise ApprovalError(f"approval.{name} must be a string or null")
        try:
            issued = _aware(datetime.fromisoformat(doc["issued_at"]))
            expires = _aware(datetime.fromisoformat(doc["expires_at"]))
        except (TypeError, ValueError) as exc:
            raise ApprovalError("approval timestamps must be RFC 3339 with an offset") from exc
        for name in ("approval_id", "approver", "key"):
            try:
                require_identifier(doc[name], f"approval.{name}")
            except (ValueError, InvalidIdentifierError) as exc:
                raise ApprovalError(str(exc)) from exc
        if doc["subject"] is not None:
            try:
                require_identifier(doc["subject"], "approval.subject")
            except (ValueError, InvalidIdentifierError) as exc:
                raise ApprovalError(str(exc)) from exc
        # Every remaining field has a fixed grammar, so nothing unbounded or free-form is
        # ever stored from an artefact, verified or not.
        for name, pattern in _GRAMMAR.items():
            value = doc[name]
            if value is not None and not pattern.fullmatch(value):
                raise ApprovalError(f"approval.{name} does not match its grammar")
        return cls(
            doc["journal_id"],
            doc["approval_id"],
            doc["approver"],
            doc["fingerprint"],
            doc["key"],
            doc["subject"],
            doc["amount"],
            doc["currency"],
            issued,
            expires,
            doc["signature"],
        )


def _aware(at: datetime) -> datetime:
    """Signed timestamps are normalised to UTC (`+00:00`), so any rendering of the same
    instant (`Z`, another offset) verifies against the same bytes."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("naive timestamp")
    return at.astimezone(UTC)


# --------------------------------------------------------------------- keys


def generate_signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def signing_key_from_bytes(raw: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(raw)


def verification_key_text(private: Ed25519PrivateKey) -> str:
    """The base64url raw public key, as stored in ``definition.approval_key``."""
    from cryptography.hazmat.primitives import serialization

    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return _b64(raw)


def verification_key(text: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(_unb64(text))


# ------------------------------------------------------------------- issuing


def issue(
    private: Ed25519PrivateKey,
    *,
    journal_id: str,
    approval_id: str,
    approver: str,
    fingerprint: str,
    key: str,
    issued_at: datetime,
    expires_at: datetime,
    subject: str | None = None,
    amount: str | None = None,
    currency: str | None = None,
) -> Approval:
    unsigned = Approval(
        journal_id,
        approval_id,
        approver,
        fingerprint,
        key,
        subject,
        amount,
        currency,
        _aware(issued_at),
        _aware(expires_at),
        "",
    )
    signature = _b64(private.sign(canonical_bytes(unsigned.signed_payload())))
    return replace(unsigned, signature=signature)


# ------------------------------------------------------------------ checking


def signature_verifies(approval: Approval, public: Ed25519PublicKey) -> bool:
    """Check 1 alone."""
    try:
        public.verify(_unb64(approval.signature), canonical_bytes(approval.signed_payload()))
    except (InvalidSignature, ValueError):
        return False
    return True


def check(
    approval: Approval,
    *,
    public: Ed25519PublicKey,
    now: datetime,
    journal_id: str,
    fingerprint: str,
    key: str,
) -> CheckResult:
    """Checks 1 to 3, in order, short-circuiting: the first failure is the result."""
    if not signature_verifies(approval, public):
        return "approval_invalid"
    if approval.expires_at <= now:
        return "approval_expired"
    if (approval.journal_id, approval.fingerprint, approval.key) != (journal_id, fingerprint, key):
        return "approval_scope_mismatch"
    return "checks_passed"
