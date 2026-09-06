# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Signed requests: the ``auth`` envelope of docs/spec/principals.md.

The signature is Ed25519 over the JCS bytes of a document constructed from the request
(``tool``, ``call_id``, ``arguments``, ``key``, ``approval``, absent members as ``null``),
the journal id and the envelope's ``principal`` and ``expires_at``. The clockless checks
(shape, live signer, signature) live here and run at admission; the clock checks (expiry,
the expiry bound, replay) run in the journal at its single reading.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ledgergate.codec import canonical_bytes
from ledgergate.journal.approvals import (
    generate_signing_key,
    signing_key_from_bytes,
    verification_key,
    verification_key_text,
)
from ledgergate.ledger import InvalidIdentifierError
from ledgergate.ledger.identifiers import require_identifier

ENVELOPE_FIELDS = frozenset({"principal", "expires_at", "signature"})
MAX_EXPIRES_AT_CHARS = 64
MAX_EXPIRY_SECONDS = 86_400
"""A request may be valid for at most a day; the journal refuses a longer window as
``request_expiry_unbounded`` at its clock reading (principals.md, *Keys*)."""
SIGN_CAP_SECONDS = 86_340
"""What ``ledgergate sign`` caps ``--expires-in`` at: a minute inside the journal's bound, so
a signer's clock a little ahead of the journal's is not refused at the maximum."""
_SIGNATURE = re.compile(r"[A-Za-z0-9_-]{86}")

__all__ = [
    "ENVELOPE_FIELDS",
    "MAX_EXPIRY_SECONDS",
    "SIGN_CAP_SECONDS",
    "Attribution",
    "AuthError",
    "generate_signing_key",
    "sign_request",
    "signed_document",
    "signing_key_from_bytes",
    "verification_key",
    "verification_key_text",
    "verify_envelope",
]


class AuthError(Exception):
    """A clockless envelope failure; ``code`` is the admission cause it becomes."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Attribution:
    """Who an invocation is: ``transport`` (the session's principal), ``signed`` (a verified
    envelope, with the three fields stored as evidence) or ``rejected`` (an envelope that
    failed a clockless check; the row is the session's and the claim is content)."""

    principal: str
    authentication: str
    auth_principal: str | None = None
    auth_expires_at: datetime | None = None
    auth_signature: str | None = None

    @staticmethod
    def transport(principal: str) -> Attribution:
        return Attribution(principal, "transport")

    @staticmethod
    def rejected(session_principal: str) -> Attribution:
        return Attribution(session_principal, "rejected")

    def columns(self) -> tuple[str, str, str | None, str | None, str | None]:
        return (
            self.principal,
            self.authentication,
            self.auth_principal,
            None if self.auth_expires_at is None else self.auth_expires_at.isoformat(),
            self.auth_signature,
        )


def signed_document(
    request: dict[str, Any], *, journal_id: str, principal: str, expires_at: str
) -> dict[str, Any]:
    """The document the signature covers: the request exactly as delivered minus ``auth``
    (absent is absent, not null: any member a third party attaches, an explicit null included,
    breaks the signature rather than producing a refusal attributed to the signer), plus the
    journal id and the envelope's principal and expiry."""
    doc: dict[str, Any] = {k: v for k, v in request.items() if k != "auth"}
    doc["journal_id"] = journal_id
    doc["principal"] = principal
    doc["expires_at"] = expires_at
    return doc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_request(
    request: dict[str, Any],
    *,
    private: Ed25519PrivateKey,
    journal_id: str,
    principal: str,
    expires_at: datetime,
) -> dict[str, Any]:
    """The ``auth`` envelope for ``request`` (which must not already carry one)."""
    if expires_at.tzinfo is None:
        raise ValueError("expires_at must carry a timezone")
    stamp = expires_at.astimezone(UTC).isoformat()
    doc = signed_document(request, journal_id=journal_id, principal=principal, expires_at=stamp)
    signature = _b64(private.sign(canonical_bytes(doc)))
    return {"principal": principal, "expires_at": stamp, "signature": signature}


_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})")


def _parse_expires_at(text: Any) -> datetime:
    """RFC 3339 with an offset (`Z` or `+hh:mm`), the extended form only: the signature covers
    the text, so the grammar is fixed rather than whatever a parser tolerates."""
    if not isinstance(text, str) or len(text) > MAX_EXPIRES_AT_CHARS:
        raise AuthError("authentication_malformed")
    if not _RFC3339.fullmatch(text):
        raise AuthError("authentication_malformed")
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        # OverflowError: a shape-valid stamp at the calendar's edge has no UTC rendering
        raise AuthError("authentication_malformed") from exc


def verify_envelope(
    value: dict[str, Any],
    envelope: Any,
    *,
    signers: dict[str, Ed25519PublicKey],
    journal_id: str,
) -> tuple[str, datetime, str]:
    """The clockless checks. Returns ``(principal, expires_at, signature)`` for a verified
    envelope, else raises ``AuthError`` with the cause. ``value`` is the request *without*
    the ``auth`` member; ``signers`` the live signed principals."""
    if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_FIELDS:
        raise AuthError("authentication_malformed")
    principal = envelope["principal"]
    signature = envelope["signature"]
    if not isinstance(principal, str) or not isinstance(signature, str):
        raise AuthError("authentication_malformed")
    try:
        require_identifier(principal, "principal")
    except InvalidIdentifierError as exc:
        raise AuthError("authentication_malformed") from exc
    if not _SIGNATURE.fullmatch(signature) or _b64(_unb64(signature)) != signature:
        raise AuthError("authentication_malformed")  # canonical base64url only: one spelling
    expires_at = _parse_expires_at(envelope["expires_at"])
    public = signers.get(principal)
    if public is None:
        raise AuthError("unknown_principal")
    doc = signed_document(
        value, journal_id=journal_id, principal=principal, expires_at=envelope["expires_at"]
    )
    try:
        public.verify(_unb64(signature), canonical_bytes(doc))
    except InvalidSignature as exc:
        raise AuthError("bad_signature") from exc
    return principal, expires_at, signature


def expiry_cause(expires_at: datetime, now: datetime) -> str | None:
    """The clock check at the journal's single reading: expired, or valid for longer than the
    bound; ``None`` when the envelope is in its window."""
    if expires_at <= now:
        return "request_expired"
    if expires_at - now > timedelta(seconds=MAX_EXPIRY_SECONDS):  # a subtraction never overflows
        return "request_expiry_unbounded"
    return None
