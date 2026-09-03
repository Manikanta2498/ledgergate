"""Helpers for journal tests: a USD chart and request builders."""

from __future__ import annotations

import sqlite3
from typing import Any

from ledgergate.ledger import (
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
)

CHART = ChartOfAccounts(
    [
        Account("cash", AccountType.ASSET, USD),
        Account("revenue", AccountType.REVENUE, USD),
        Account("wallet", AccountType.LIABILITY, USD, allow_negative=False),
    ]
)


def sale_doc(amount: int = 1999, **extra: Any) -> dict[str, Any]:
    return {
        "postings": [
            {"account": "cash", "side": "debit", "money": {"amount": amount, "currency": "USD"}},
            {
                "account": "revenue",
                "side": "credit",
                "money": {"amount": amount, "currency": "USD"},
            },
        ],
        **extra,
    }


def post(key: str, call_id: str | None = None, amount: int = 1999, **extra: Any) -> dict[str, Any]:
    return {
        "tool": "post",
        "call_id": call_id or f"call-{key}",
        "key": key,
        "arguments": {"draft": sale_doc(amount, **extra)},
    }


def open_txn(key: str, txn: str, amount: int = 1000) -> dict[str, Any]:
    return {
        "tool": "open_transaction",
        "call_id": f"call-{key}",
        "key": key,
        "arguments": {"transaction_id": txn, "amount": {"amount": amount, "currency": "USD"}},
    }


def balance(account: str, call_id: str = "call-read") -> dict[str, Any]:
    return {"tool": "balance", "call_id": call_id, "arguments": {"account": account}}


def rows(
    conn: sqlite3.Connection, table: str, where: str = "1=1", *params: Any
) -> list[tuple[Any, ...]]:
    return conn.execute(
        f"SELECT * FROM {table} WHERE {where} ORDER BY journal_sequence", params
    ).fetchall()


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
