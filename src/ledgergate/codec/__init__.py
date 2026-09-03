# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Encodings shared by every layer above the core.

Three things live here because both ``ledgergate.trace`` and ``ledgergate.journal`` need
them and neither may import the other:

- :mod:`~ledgergate.codec.commands`: the one JSON form of a ledger ``Command``, with a
  round-trip invariant against :func:`ledgergate.ledger.command_fingerprint`.
- :mod:`~ledgergate.codec.jcs`: RFC 8785 JSON Canonicalization Scheme, the serialization
  every digest in the journal is computed over.
- :mod:`~ledgergate.codec.ijson`: an RFC 7493 I-JSON decoder, the only entry point through
  which untrusted JSON reaches admission.
- :mod:`~ledgergate.codec.tokens`: keyed tokenization of caller identifiers and fail-closed
  redaction of free text, shared by the journal's admitter and the trace recorder.

This package imports the standard library and the core, nothing else.
"""

from __future__ import annotations

from ledgergate.codec.commands import (
    CODEC_VERSION,
    CodecError,
    decode_command,
    decode_draft,
    encode_command,
    encode_draft,
)
from ledgergate.codec.ijson import (
    MAX_SAFE_INTEGER,
    IJsonError,
    loads,
    require_ijson,
)
from ledgergate.codec.jcs import (
    JcsError,
    canonical_bytes,
    canonical_text,
    digest,
)
from ledgergate.codec.tokens import (
    DOMAIN_PATTERN,
    MIN_KEY_BYTES,
    REDACTION_PATTERN,
    TOKEN_PATTERN,
    Tokenizer,
)

__all__ = [
    "CODEC_VERSION",
    "DOMAIN_PATTERN",
    "MAX_SAFE_INTEGER",
    "MIN_KEY_BYTES",
    "REDACTION_PATTERN",
    "TOKEN_PATTERN",
    "CodecError",
    "IJsonError",
    "JcsError",
    "Tokenizer",
    "canonical_bytes",
    "canonical_text",
    "decode_command",
    "decode_draft",
    "digest",
    "encode_command",
    "encode_draft",
    "loads",
    "require_ijson",
]
