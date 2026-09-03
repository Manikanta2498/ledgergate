# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Keyed tokenization and fail-closed redaction, per ``docs/spec/identifiers-and-redaction.md``.

One :class:`Tokenizer` serves both the journal's admitter and the trace recorder, so a
command is transformed identically whichever path records it: caller identifiers become
``tk1_<domain>_<hmac>`` tokens on every reference, free text becomes a deterministic
``rd1_<domain>_<hmac>`` replacement, amounts, currencies, sides and account references
stay in the clear. Equal inputs transform equally under one key, so a later ``settle``
finds the transaction an earlier ``open_transaction`` stored, and a retry with the raw key
replays. The key never leaves this object and is never written anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Any

from ledgergate.codec.jcs import canonical_bytes, canonical_text
from ledgergate.ledger import (
    Advance,
    Command,
    EntryDraft,
    OpenTransaction,
    Post,
    Refund,
    Reverse,
)
from ledgergate.ledger.identifiers import require_identifier

DOMAIN_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
TOKEN_PATTERN = re.compile(r"^tk1_[a-z0-9-]{1,32}_[A-Za-z0-9_-]{43}$")
REDACTION_PATTERN = re.compile(r"^rd1_[a-z0-9-]{1,32}_[A-Za-z0-9_-]{43}$")
MIN_KEY_BYTES = 16


class Tokenizer:
    """Keyed HMAC-SHA256 tokenization under one domain and key version."""

    __slots__ = ("_key", "domain", "key_version")

    def __init__(self, key: bytes, *, domain: str, key_version: str) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"token key must be at least {MIN_KEY_BYTES} bytes")
        if not DOMAIN_PATTERN.match(domain):
            raise ValueError(f"token domain {domain!r} does not match {DOMAIN_PATTERN.pattern}")
        require_identifier(key_version, "key version")
        self._key = bytes(key)
        self.domain = domain
        self.key_version = key_version

    def __repr__(self) -> str:  # never the key
        return f"Tokenizer(domain={self.domain!r}, key_version={self.key_version!r})"

    # ------------------------------------------------------------- primitives

    def _mac(self, purpose: str, payload: bytes) -> str:
        digest = hmac.new(self._key, purpose.encode() + b"\x00" + payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def tokenize(self, raw: str) -> str:
        """A class-2 identifier's token. Validates the raw value as an identifier first and
        the token once more after construction, so a token is always a valid identifier."""
        require_identifier(raw, "identifier")
        token = f"tk1_{self.domain}_{self._mac(self.domain, raw.encode('utf-8'))}"
        if not TOKEN_PATTERN.match(token):  # pragma: no cover - by construction
            raise ValueError("token construction produced an ill-formed token")
        return require_identifier(token, "token")

    def redact(self, text: str) -> str:
        """A class-1 free-text field's deterministic replacement. Empty stays empty: there
        is nothing to protect and the ledger treats "" as "no description"."""
        if text == "":
            return ""
        return f"rd1_{self.domain}_{self._mac(self.domain + ':text', text.encode('utf-8'))}"

    def key_check(self) -> str:
        """A value that identifies the key without revealing it: a journal stores it at
        creation and refuses at open an admitter whose key does not reproduce it. Not
        reversible for a random key of at least 16 bytes."""
        return self._mac(self.domain + ":keycheck", b"")

    def digest_input(self, value: Any) -> str:
        """A keyed digest over the canonical form of raw, rejected input: it commits to the
        input without being reversible by dictionary."""
        return hmac.new(
            self._key,
            self.domain.encode() + b"\x00input\x00" + canonical_bytes(value),
            hashlib.sha256,
        ).hexdigest()

    # -------------------------------------------------------------- documents

    def redact_json(self, value: Any) -> Any:
        """Fail-closed redaction of untyped JSON (tool arguments and results in a trace).
        Every string, whether an object key or a leaf, and every number is replaced by a
        redaction token (a card or phone number is a number as often as a string); booleans
        and null are kept, as is structure. Nothing in a trace replays a tool payload, so
        nothing depends on the original values."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int):
            return self.redact(str(value))  # any integer the v1 schema accepts, JCS range or not
        if isinstance(value, float):
            return self.redact(canonical_text(value))
        if isinstance(value, Mapping):
            return {self.redact(str(k)): self.redact_json(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_json(v) for v in value]
        return value

    def draft(self, draft: EntryDraft) -> EntryDraft:
        """Description and tags (keys *and* values: both are caller text) redacted; tags
        re-sorted over the stored form so the fingerprint is over what is stored."""
        return EntryDraft(
            draft.postings,
            self.redact(draft.description),
            tuple(sorted((self.redact(k), self.redact(v)) for k, v in draft.tags)),
        )

    def command(self, command: Command) -> Command:
        """The same command with every caller identifier tokenized and every free-text
        field redacted. Amounts, currencies, sides, accounts and the entry reference of a
        ``reverse`` are unchanged; the key is tokenized like any other identifier."""
        match command:
            case Post(key, draft):
                return Post(self.tokenize(key), self.draft(draft))
            case Reverse(key, entry_id, description):
                return Reverse(self.tokenize(key), entry_id, self.redact(description))
            case OpenTransaction(key, transaction_id, amount):
                return OpenTransaction(self.tokenize(key), self.tokenize(transaction_id), amount)
            case Advance(key, transaction_id, event, entry):
                return Advance(
                    self.tokenize(key),
                    self.tokenize(transaction_id),
                    event,
                    None if entry is None else self.draft(entry),
                )
            case Refund(key, transaction_id, money, entry):
                return Refund(
                    self.tokenize(key),
                    self.tokenize(transaction_id),
                    money,
                    None if entry is None else self.draft(entry),
                )

    def arguments(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The admitted form of a write tool's ``arguments`` document, field by field under
        the command's classes. Fields whose type is wrong are left for the codec to refuse
        structurally; unknown fields likewise. ``tool`` is the command kind."""
        del tool  # every write tool shares these field names; the codec checks which apply
        out = dict(arguments)
        if isinstance(tid := out.get("transaction_id"), str):
            out["transaction_id"] = self.tokenize(tid)
        if isinstance(desc := out.get("description"), str):
            out["description"] = self.redact(desc)
        for name in ("draft", "entry"):
            if isinstance(doc := out.get(name), dict):
                out[name] = self._draft_doc(doc)
        return out

    def _draft_doc(self, doc: dict[str, Any]) -> dict[str, Any]:
        out = dict(doc)
        if isinstance(desc := out.get("description"), str):
            out["description"] = self.redact(desc)
        if isinstance(tags := out.get("tags"), dict):
            out["tags"] = {
                self.redact(k): self.redact(v) if isinstance(v, str) else v for k, v in tags.items()
            }
        return out


_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_LONG_DIGIT_RUN = re.compile(r"(?:\d[ -]?){10,}")


def looks_sensitive(value: str) -> bool:
    """A cheap, conservative guess for operator-defined names: an email address, or a run
    of ten or more digits (a phone or card number). Used only to *warn*; the operator owns
    what they name their accounts."""
    return bool(_EMAIL.search(value) or _LONG_DIGIT_RUN.search(value))
