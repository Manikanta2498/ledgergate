# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The MCP runtime (M4): one journal as tools over stdio, per ``docs/spec/mcp-runtime.md``.

A transport and nothing more: decode a wire line with the project's I-JSON decoder, hand the
journal one untyped value, encode what the journal committed. This package imports
``journal``, ``ledger`` and ``codec`` and never ``trace``, ``derive``, ``invariants`` or
``runner`` (a ``forbidden`` import-linter contract enforces it).
"""

from __future__ import annotations

from ledgergate.mcp.effects import RandomIds, SystemClock
from ledgergate.mcp.server import (
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    Server,
    request_for_call,
    serve,
)
from ledgergate.mcp.tools import TOOL_SCHEMAS, tool_list

__all__ = [
    "MAX_LINE_BYTES",
    "PROTOCOL_VERSION",
    "TOOL_SCHEMAS",
    "RandomIds",
    "Server",
    "SystemClock",
    "request_for_call",
    "serve",
    "tool_list",
]
