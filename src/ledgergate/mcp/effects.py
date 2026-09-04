# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The process's effects: a UTC wall clock and a random id generator. Both satisfy the core's
protocols; the journal rejects a naive clock and a repeated or invalid id as effect faults."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class RandomIds:
    """128 random bits per id under a fixed prefix: fresh across processes without
    coordination, which is the journal's multi-process precondition."""

    def __init__(self, prefix: str = "entry") -> None:
        self._prefix = prefix

    def next_id(self) -> str:
        return f"{self._prefix}-{secrets.token_hex(16)}"
