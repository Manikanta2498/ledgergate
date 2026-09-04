"""The MCP runtime against docs/spec/mcp-runtime.md: wire decoding, the request/notification
split, the tools/call -> Request mapping, the response shape, failure routing, the stderr
vocabulary, the line bound, and the capacity check."""

from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from ledgergate.codec import IJsonError, loads
from ledgergate.journal import ConfigurationError, IntegrityError, Journal, JournalError
from ledgergate.ledger import (
    EPOCH,
    USD,
    Account,
    AccountType,
    ChartOfAccounts,
    SequentialIds,
    SteppingClock,
)
from ledgergate.mcp import MAX_LINE_BYTES, PROTOCOL_VERSION, Server, request_for_call, serve
from ledgergate.mcp.tools import TOOL_SCHEMAS, tool_list

CHART = ChartOfAccounts(
    [Account("cash", AccountType.ASSET, USD), Account("revenue", AccountType.REVENUE, USD)]
)
DRAFT = {
    "postings": [
        {"account": "cash", "side": "debit", "money": {"amount": 5, "currency": "USD"}},
        {"account": "revenue", "side": "credit", "money": {"amount": 5, "currency": "USD"}},
    ]
}


def _journal(tmp_path: Path) -> Journal:
    return Journal.create(
        str(tmp_path / "j.journal"), CHART, clock=SteppingClock(EPOCH), ids=SequentialIds()
    )


def _run(journal: Journal, *lines: Any) -> tuple[list[dict[str, Any]], list[str], int]:
    raw = b"".join((m if isinstance(m, bytes) else json.dumps(m).encode()) + b"\n" for m in lines)
    out, err = io.StringIO(), io.StringIO()
    code = Server(journal, out, err).run(io.BytesIO(raw))
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    return responses, err.getvalue().splitlines(), code


def _call(rpc_id: Any, name: Any, arguments: Any = None, **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    params.update(extra)
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": params}


class TestDecoderTotality:
    def test_deep_brackets_and_long_digits_are_ijson_errors(self) -> None:
        with pytest.raises(IJsonError, match="nesting"):
            loads("[" * 100_000 + "]" * 100_000)
        with pytest.raises(IJsonError, match="safe range"):
            loads("1" * 5000)
        with pytest.raises(IJsonError):
            loads("1" * 18)
        assert loads("9007199254740991") == 2**53 - 1


class TestWireGrammar:
    def test_malformed_lines_are_answered_unrecorded(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        responses, err, code = _run(
            j,
            b"not json",
            {"foo": "boo"},
            [1, 2],
            {"jsonrpc": "2.0", "id": None, "method": "ping"},
            {"jsonrpc": "2.0", "id": True, "method": "ping"},
            {"jsonrpc": "2.0", "id": 9, "method": 7},
            {"jsonrpc": "2.0", "id": 10, "method": "nope"},
            {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": "x"},
            {"jsonrpc": "2.0", "id": 12, "method": "tools/call"},
        )
        codes = [(r["id"], r["error"]["code"]) for r in responses]
        assert codes == [
            (None, -32700),
            (None, -32600),
            (None, -32600),
            (None, -32600),
            (None, -32600),
            (9, -32600),  # id echoed when a valid one was decoded
            (10, -32601),
            (11, -32602),
            (12, -32602),
        ]
        conn = sqlite3.connect(j.path)
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 0
        conn.close()
        assert code == 0 and len(err) == len(responses)
        j.close()

    def test_notifications_are_never_answered_and_a_call_notification_does_not_run(
        self, tmp_path: Path
    ) -> None:
        j = _journal(tmp_path)
        responses, err, _ = _run(
            j,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "post", "arguments": {"idempotency_key": "k", "draft": DRAFT}},
            },
            {"jsonrpc": "2.0", "id": 1, "method": "notifications/initialized"},
        )
        assert [r.get("error", {}).get("code") for r in responses] == [-32601]
        assert err[0].endswith("id=absent method=tools/call unanswerable") and len(err) == 2
        conn = sqlite3.connect(j.path)
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 0
        conn.close()
        j.close()

    def test_initialize_always_answers_the_servers_version(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        responses, _, _ = _run(
            j,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        )
        assert responses[0]["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert responses[0]["result"]["capabilities"] == {"tools": {}}
        assert responses[1]["result"] == {}
        j.close()

    def test_a_call_before_initialize_is_served_and_recorded(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        responses, _, _ = _run(j, _call(1, "trial_balance"))
        assert responses[0]["result"]["isError"] is False
        j.close()


class TestMapping:
    def test_step_4_value_shape(self) -> None:
        v = request_for_call(
            7,
            {
                "name": "post",
                "arguments": {"idempotency_key": "k", "approval": {"a": 1}, "draft": DRAFT},
                "_meta": {"x": 1},
            },
        )
        assert v == {
            "call_id": "rpc-n7",
            "tool": "post",
            "key": "k",
            "approval": {"a": 1},
            "arguments": {"draft": DRAFT},
        }
        assert request_for_call("7", {"name": "post"}) == {"call_id": "rpc-s7", "tool": "post"}
        assert request_for_call(1, {}) == {"call_id": "rpc-n1"}
        assert request_for_call(1, {"name": "post", "arguments": "x"}) == {
            "call_id": "rpc-n1",
            "tool": "post",
            "arguments": "x",
        }
        assert request_for_call(1, {"name": "post", "arguments": {"idempotency_key": None}}) == {
            "call_id": "rpc-n1",
            "tool": "post",
            "key": None,
            "arguments": {},
        }

    def test_every_malformed_call_shape_is_recorded_invalid(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        responses, _, _ = _run(
            j,
            _call(1, "nope"),
            _call(2, 7),
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}},
            _call(4, "post", "not-an-object"),
            _call(5, "post", {"draft": DRAFT}),  # missing key
            _call(6, "post", {"idempotency_key": None, "draft": DRAFT}),
            _call(7, "balance", {"account": "cash", "idempotency_key": "k"}),
            _call(
                8, "post", {"idempotency_key": "k8", "approval": None, "draft": DRAFT}
            ),  # null approval = absent
            _call(
                "x ", "trial_balance"
            ),  # id renders to a non-identifier: recorded, unrecoverable call id
        )
        results = [r["result"]["structuredContent"] for r in responses]
        errors = [x["error"]["message"] if not x["ok"] else "ok" for x in results]
        assert errors == [
            "unknown_tool at tool",
            "wrong_type at tool",
            "missing_field at tool",
            "wrong_type at arguments",
            "missing_field at key",
            "wrong_type at key",
            "unexpected_field at key",
            "ok",
            "invalid_identifier at call_id",
        ]
        assert all(
            r["result"]["isError"] == (not x["ok"]) for r, x in zip(responses, results, strict=True)
        )
        conn = sqlite3.connect(j.path)
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 9
        conn.close()
        j.close()

    def test_replay_conflict_and_read_through_the_transport(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        responses, _, _ = _run(
            j,
            _call(1, "post", {"idempotency_key": "k1", "draft": DRAFT}),
            _call("1", "post", {"idempotency_key": "k1", "draft": DRAFT}),
            _call(2, "post", {"idempotency_key": "k1", "draft": {**DRAFT, "description": "x"}}),
            _call(3, "balance", {"account": "cash"}),
        )
        first, replay, conflict, read = (r["result"]["structuredContent"] for r in responses)
        assert first["ok"] and replay["ok"] and replay["result"]["replayed"] is True
        assert replay["result"]["head"] == first["result"]["head"]
        assert conflict["error"]["type"] == "IdempotencyConflictError"
        assert read["result"]["balance"] == {"amount": "5", "currency": "USD"}
        text = responses[0]["result"]["content"][0]["text"]
        assert json.loads(text) == first  # one value, two renderings
        j.close()


class TestFailureRouting:
    def test_journal_error_is_32000_and_the_session_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        j = _journal(tmp_path)

        def boom(_value: Any) -> Any:
            raise JournalError("journal unavailable: locked")

        monkeypatch.setattr(j, "handle", boom)
        responses, err, code = _run(
            j, _call(1, "trial_balance"), {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        )
        assert responses[0]["error"]["code"] == -32000
        assert responses[0]["error"]["data"] == {
            "class": "JournalError",
            "message": "journal unavailable: locked",
        }
        assert responses[1]["result"] == {} and code == 0
        assert err[0].endswith("id=integer method=tools/call JournalError")
        j.close()

    def test_integrity_error_answers_then_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        j = _journal(tmp_path)

        def boom(_value: Any) -> Any:
            raise IntegrityError("the rows contradict")

        monkeypatch.setattr(j, "handle", boom)
        responses, err, code = _run(
            j, _call(1, "trial_balance"), {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        )
        assert [r["error"]["code"] for r in responses] == [-32000] and code == 3
        assert err[-1].endswith("IntegrityError")
        j.close()

    def test_a_module_raised_programming_error_takes_the_bug_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        j = _journal(tmp_path)
        conn = j._conn

        def boom(_value: Any) -> Any:
            conn.execute("SELECT ?", (1, 2))  # incorrect number of bindings: ProgrammingError
            raise AssertionError("unreachable")

        monkeypatch.setattr(j, "handle", boom)
        responses, err, code = _run(j, _call(1, "trial_balance"))
        assert responses[0]["error"] == {"code": -32603, "message": "internal error"} and code == 4
        assert err[-1].endswith("internal")
        j.close()

    def test_a_bug_is_32603_without_data_and_exits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        j = _journal(tmp_path)

        def boom(_value: Any) -> Any:
            raise KeyError("secret-caller-key")

        monkeypatch.setattr(j, "handle", boom)
        responses, err, code = _run(j, _call(1, "trial_balance"))
        assert responses == [
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "internal error"}}
        ]
        assert code == 4 and err[-1].endswith("internal") and "secret" not in "".join(err)
        j.close()

    def test_stderr_never_carries_caller_content(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        _, err, _ = _run(
            j,
            {"jsonrpc": "2.0", "id": "SECRET-ID", "method": "SECRET-METHOD"},
            _call("SECRET-ID-2", "SECRET-TOOL"),
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": "SECRET"},
        )
        joined = "\n".join(err)
        assert "SECRET" not in joined
        assert "id=string method=unknown" in err[0]
        j.close()


class TestLineBound:
    def test_an_oversized_line_is_one_parse_error_and_cannot_smuggle(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        big = (
            b'{"jsonrpc":"2.0","id":1,"method":"ping","pad":"'
            + b"x" * (MAX_LINE_BYTES + 100)
            + b'"}\n'
        )
        tail = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode() + b"\n"
        out, err = io.StringIO(), io.StringIO()
        Server(j, out, err).run(io.BytesIO(big + tail))
        responses = [json.loads(line) for line in out.getvalue().splitlines()]
        assert [r.get("error", {}).get("code", "ok") for r in responses] == [-32700, "ok"]
        assert responses[1]["id"] == 2
        assert f"bytes={len(big)}" in err.getvalue() and "x" * 50 not in err.getvalue()
        j.close()

    def test_the_bound_is_on_content_with_or_without_a_terminator(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        prefix = b'{"jsonrpc":"2.0","id":5,"method":"ping","pad":"'
        suffix = b'"}'
        exact = prefix + b"x" * (MAX_LINE_BYTES - len(prefix) - len(suffix)) + suffix
        assert len(exact) == MAX_LINE_BYTES
        for payload in (exact + b"\n", exact):  # exactly at the bound: a message either way
            out = io.StringIO()
            Server(j, out, io.StringIO()).run(io.BytesIO(payload))
            assert json.loads(out.getvalue())["result"] == {}
        over = prefix + b"x" * (MAX_LINE_BYTES - len(prefix) - len(suffix) + 1) + suffix
        for payload in (over + b"\n", over):  # one byte over: refused either way
            out = io.StringIO()
            Server(j, out, io.StringIO()).run(io.BytesIO(payload))
            assert json.loads(out.getvalue())["error"]["code"] == -32700
        j.close()

    def test_blank_and_whitespace_lines_are_parse_errors(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        responses, _, _ = _run(j, b"", b"   ", b"\r")
        assert [r["error"]["code"] for r in responses] == [-32700, -32700, -32700]
        j.close()


class TestTools:
    def test_schemas_carry_the_reserved_members_and_validate_examples(self) -> None:
        import jsonschema

        for tool, schema in TOOL_SCHEMAS.items():
            props = schema["properties"]
            if tool in ("balance", "trial_balance"):
                assert "idempotency_key" not in props and "approval" not in props
            else:
                assert "idempotency_key" in schema["required"] and "approval" in props
        examples: dict[str, dict[str, Any]] = {
            "post": {"idempotency_key": "k", "draft": DRAFT},
            "reverse": {"idempotency_key": "k", "entry_id": "e"},
            "open_transaction": {
                "idempotency_key": "k",
                "transaction_id": "t",
                "amount": {"amount": 1, "currency": "USD"},
            },
            "advance": {"idempotency_key": "k", "transaction_id": "t", "event": "authorize"},
            "refund": {
                "idempotency_key": "k",
                "transaction_id": "t",
                "money": {"amount": 1, "currency": "USD"},
                "entry": DRAFT,
            },
            "balance": {"account": "cash"},
            "trial_balance": {},
        }
        for tool, example in examples.items():
            jsonschema.validate(example, TOOL_SCHEMAS[tool])
        assert [t["name"] for t in tool_list()] == sorted(TOOL_SCHEMAS)
        # every bound in a schema is the codec's constant, not a copy
        from ledgergate.codec import MAX_POSTINGS, MAX_TAGS, MAX_TEXT

        draft = TOOL_SCHEMAS["post"]["properties"]["draft"]["properties"]
        assert draft["postings"]["maxItems"] == MAX_POSTINGS
        assert draft["description"]["maxLength"] == MAX_TEXT
        assert draft["tags"]["maxProperties"] == MAX_TAGS
        assert TOOL_SCHEMAS["reverse"]["properties"]["description"]["maxLength"] == MAX_TEXT


class TestJournalChanges:
    def test_capacity_is_refused_under_the_lock_for_every_transaction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ledgergate.journal import CapacityError, store

        j = _journal(tmp_path)
        assert (
            j.handle(
                {"tool": "post", "call_id": "c1", "key": "k1", "arguments": {"draft": DRAFT}}
            ).response
            == "applied"
        )
        monkeypatch.setattr(store, "MAX_TRACE_EVENTS", 9 + 1)  # one invocation, one message
        assert j.record_message("user", "fits") > 0
        with pytest.raises(CapacityError, match="capacity"):
            j.handle(
                {"tool": "trial_balance", "call_id": "c2", "arguments": {}}
            )  # a read is an invocation
        with pytest.raises(CapacityError):
            j.record_message("user", "no room")
        conn = sqlite3.connect(j.path)
        assert conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0] == 1
        conn.close()
        j.close()

    def test_a_non_identifier_principal_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="principal"):
            Journal.create(
                str(tmp_path / "p.journal"),
                CHART,
                clock=SteppingClock(EPOCH),
                ids=SequentialIds(),
                principal="a b\n",
            )

    def test_sqlite_errors_are_classified_by_mechanism(self) -> None:
        from ledgergate.journal.store import _classify

        assert type(_classify(sqlite3.IntegrityError("CHECK failed"), "w")) is IntegrityError
        prog = sqlite3.ProgrammingError("Incorrect number of bindings")
        assert _classify(prog, "w") is prog  # a bug: re-raised unmapped, before any code is read
        busy = sqlite3.OperationalError("database is locked")
        assert type(_classify(busy, "w")) is JournalError
        corrupt = sqlite3.DatabaseError("file is not a database")
        corrupt.sqlite_errorcode = sqlite3.SQLITE_NOTADB
        assert type(_classify(corrupt, "w")) is IntegrityError

    def test_capacity_check_uses_the_indexes(self, tmp_path: Path) -> None:
        j = _journal(tmp_path)
        conn = sqlite3.connect(j.path)
        plan = " ".join(
            str(r)
            for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM events WHERE invocation IS NULL"
            )
        )
        assert "events_messages" in plan
        plan = " ".join(
            str(r) for r in conn.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM invocations")
        )
        assert "invocations_disposition" in plan
        conn.close()
        j.close()


def test_serve_closes_the_journal_and_returns_the_code(tmp_path: Path) -> None:
    j = _journal(tmp_path)
    code = serve(
        j,
        stdin=io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n'),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    assert code == 0
    with pytest.raises(Exception):  # noqa: B017 - the connection is closed, whatever sqlite says
        j.handle({"tool": "trial_balance", "call_id": "c", "arguments": {}})


class TestServeCli:
    def _chart(self, tmp_path: Path) -> Path:
        p = tmp_path / "chart.json"
        p.write_text(
            json.dumps(
                [
                    {"account_id": "cash", "kind": "asset", "currency": "USD"},
                    {"account_id": "revenue", "kind": "revenue", "currency": "USD"},
                ]
            )
        )
        return p

    def test_flag_validation(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from ledgergate.cli.__main__ import main

        j = str(tmp_path / "j.journal")
        assert main(["serve", "--journal", j, "--principal", "a b\n"]) == 2
        assert main(["serve", "--journal", j, "--approval-key", "x"]) == 2  # only with --create
        assert main(["serve", "--journal", j, "--create"]) == 2  # needs --chart
        assert main(["serve", "--journal", j]) == 2  # does not exist
        short = tmp_path / "short.key"
        short.write_bytes(b"x" * 16)
        assert (
            main(
                [
                    "serve",
                    "--journal",
                    j,
                    "--create",
                    "--chart",
                    str(self._chart(tmp_path)),
                    "--token-key-file",
                    str(short),
                ]
            )
            == 2
        )
        err = capsys.readouterr().err
        assert "at least 32 bytes" in err and "not" in err

    def test_end_to_end_over_a_subprocess_with_policy_and_tokens(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        chart = self._chart(tmp_path)
        key = tmp_path / "tok.key"
        key.write_bytes(bytes(range(32)))
        policy = tmp_path / "policy.json"
        policy.write_text(
            json.dumps(
                {
                    "set": "ledgergate.journal.policy.ThresholdPolicySet",
                    "version": "p",
                    "deny_above": [
                        {"kind": "open_transaction", "currency": "USD", "amount": "100"}
                    ],
                    "approve_above": [],
                    "window_caps": [],
                    "gated_reads": [],
                }
            )
        )
        journal = tmp_path / "j.journal"
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            _call(
                2,
                "open_transaction",
                {
                    "idempotency_key": "raw-key",
                    "transaction_id": "raw-txn",
                    "amount": {"amount": 500, "currency": "USD"},
                },
            ),
            _call(
                3,
                "open_transaction",
                {
                    "idempotency_key": "raw-key-2",
                    "transaction_id": "raw-txn-2",
                    "amount": {"amount": 50, "currency": "USD"},
                },
            ),
        ]
        stdin = "".join(json.dumps(m) + "\n" for m in lines).encode()
        args = [
            sys.executable,
            "-m",
            "ledgergate.cli",
            "serve",
            "--journal",
            str(journal),
            "--policy",
            str(policy),
            "--token-key-file",
            str(key),
        ]
        run = subprocess.run(
            [*args, "--create", "--chart", str(chart)],
            input=stdin,
            capture_output=True,
            check=False,
        )
        assert run.returncode == 0, run.stderr
        responses = [json.loads(line) for line in run.stdout.decode().splitlines()]
        denied, applied = (r["result"]["structuredContent"] for r in responses[1:])
        assert denied["error"]["type"] == "PolicyDenied" and applied["ok"]
        assert b"raw-key" not in run.stderr
        conn = sqlite3.connect(journal)
        blob = " ".join(
            str(row)
            for table in ("operations", "events", "invocations")
            for row in conn.execute(f"SELECT * FROM {table}")
        )
        conn.close()
        assert "raw-key" not in blob and "raw-txn" not in blob
        # reopening must repeat the flags; without --policy the binding check refuses
        again = subprocess.run(
            [
                sys.executable,
                "-m",
                "ledgergate.cli",
                "serve",
                "--journal",
                str(journal),
                "--token-key-file",
                str(key),
            ],
            input=b"",
            capture_output=True,
            check=False,
        )
        assert again.returncode == 2 and b"ConfigurationError" in again.stderr
        again = subprocess.run(
            args,
            input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n",
            capture_output=True,
            check=False,
        )
        assert again.returncode == 0 and json.loads(again.stdout)["result"] == {}
        from ledgergate.cli.__main__ import main

        assert main(["verify", str(journal)]) == 0
