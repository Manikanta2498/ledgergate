# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""One rule for every identifier the ledger stores: non-empty, trimmed, single-line.

Idempotency keys, transaction ids, account ids and generated entry ids are all keys into
mappings that decide whether money moves. An empty or whitespace-padded key is not a
different spelling of a real one; it is a bug at the call site, and it is refused here
so it cannot become a duplicate payment later.
"""

from __future__ import annotations

from ledgergate.ledger.errors import InvalidIdentifierError

MAX_IDENTIFIER_LENGTH = 256


def require_identifier(value: str, what: str) -> str:
    """Return ``value`` if it is a usable identifier, else raise :class:`InvalidIdentifierError`."""
    if not isinstance(value, str):
        raise InvalidIdentifierError(what, repr(value), "must be a string")
    if not value:
        raise InvalidIdentifierError(what, value, "must not be empty")
    if value != value.strip():
        raise InvalidIdentifierError(what, value, "must not have leading or trailing whitespace")
    if any(ch in value for ch in "\r\n\x00"):
        raise InvalidIdentifierError(what, value, "must be a single line")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise InvalidIdentifierError(what, value, f"must be at most {MAX_IDENTIFIER_LENGTH} chars")
    return value
