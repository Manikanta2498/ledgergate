# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Trace schema v1: the interoperability contract, and the runtime's view of it.

``schema/trace/v1.json`` (Apache-2.0, shipped separately) is the contract. This package
reads, writes and records documents that satisfy it. Any agent, on any framework, that
emits the schema can be checked by the invariant suite without adopting this package.
"""

from __future__ import annotations

from ledgergate.trace.io import (
    SCHEMA_RELATIVE,
    SchemaNotFoundError,
    TraceError,
    default_schema_path,
    dump_trace,
    iter_schema_problems,
    load_schema,
    load_trace,
    parse_trace,
    validate_document,
    write_trace,
)
from ledgergate.trace.models import (
    SCHEMA_VERSION,
    AccountDoc,
    AdvanceDoc,
    AgentDoc,
    CommandDoc,
    CurrencyDoc,
    EntryDraftDoc,
    ErrorDoc,
    Event,
    LedgerCommandEvent,
    LedgerResultEvent,
    MessageEvent,
    MoneyDoc,
    OpenTransactionDoc,
    PostDoc,
    PostingDoc,
    RefundDoc,
    ReverseDoc,
    ToolCallEvent,
    ToolResultEvent,
    Trace,
    command_currencies,
    command_doc,
    resolve_currency,
)
from ledgergate.trace.recorder import Recorder
from ledgergate.trace.replay import Divergence, ReplayReport, replay_trace

__all__ = [
    "SCHEMA_RELATIVE",
    "SCHEMA_VERSION",
    "AccountDoc",
    "AdvanceDoc",
    "AgentDoc",
    "CommandDoc",
    "CurrencyDoc",
    "Divergence",
    "EntryDraftDoc",
    "ErrorDoc",
    "Event",
    "LedgerCommandEvent",
    "LedgerResultEvent",
    "MessageEvent",
    "MoneyDoc",
    "OpenTransactionDoc",
    "PostDoc",
    "PostingDoc",
    "Recorder",
    "RefundDoc",
    "ReplayReport",
    "ReverseDoc",
    "SchemaNotFoundError",
    "ToolCallEvent",
    "ToolResultEvent",
    "Trace",
    "TraceError",
    "command_currencies",
    "command_doc",
    "default_schema_path",
    "dump_trace",
    "iter_schema_problems",
    "load_schema",
    "load_trace",
    "parse_trace",
    "replay_trace",
    "resolve_currency",
    "validate_document",
    "write_trace",
]
