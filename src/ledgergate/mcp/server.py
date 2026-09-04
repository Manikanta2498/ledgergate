# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The stdio JSON-RPC loop and the ``tools/call`` -> ``Request`` mapping.

Every rule here is one stated in ``docs/spec/mcp-runtime.md``; the section names are in the
comments. The server holds no state the journal does not hold and decides nothing the
journal does not decide.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any

from ledgergate import __version__
from ledgergate.codec import IJsonError, canonical_text, loads
from ledgergate.journal import IntegrityError, Journal, JournalError

PROTOCOL_VERSION = "2025-06-18"
MAX_LINE_BYTES = 16 * 1024 * 1024
METHODS = frozenset({"initialize", "ping", "tools/list", "tools/call", "notifications/initialized"})
_KNOWN_CLASSES = frozenset(
    {"CapacityError", "ConfigurationError", "EffectError", "IntegrityError", "JournalError"}
)
_DATA_BOUND = 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
JOURNAL_ERROR = -32000


class SessionEndError(Exception):
    """Raised by the loop when the spec says the process must stop; carries the exit code."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ the mapping


def request_for_call(rpc_id: Any, params: Any) -> dict[str, Any]:
    """`tools/call` -> the value handed to ``Journal.handle`` (spec: *The mapping*, step 4).

    ``params`` is known to be an object here (a non-object is ``-32602`` before this). Members
    are forwarded *as given*; admission judges every one of them.
    """
    value: dict[str, Any] = {"call_id": render_call_id(rpc_id)}
    if "name" in params:
        value["tool"] = params["name"]
    if "arguments" in params:
        arguments = params["arguments"]
        if isinstance(arguments, dict):
            rest = dict(arguments)
            if "idempotency_key" in rest:
                value["key"] = rest.pop("idempotency_key")
            if "approval" in rest:
                value["approval"] = rest.pop("approval")
            value["arguments"] = rest
        else:
            value["arguments"] = arguments
    return value


def render_call_id(rpc_id: Any) -> str:
    """`rpc-n<int>` or `rpc-s<str>`: the prefix letter keeps ``7`` and ``"7"`` distinct."""
    return f"rpc-n{rpc_id}" if isinstance(rpc_id, int) else f"rpc-s{rpc_id}"


def _valid_id(value: Any) -> bool:
    # JSON booleans are not integers, whatever the host language says.
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _id_kind(message: Any) -> str:
    if not isinstance(message, dict):
        return "invalid"  # decoded, but not an object: it has no id to speak of
    if "id" not in message:
        return "absent"
    value = message["id"]
    if isinstance(value, str):
        return "string"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    return "invalid"


# ------------------------------------------------------------------ the server


@dataclass
class Server:
    journal: Journal
    stdout: IO[str] = sys.stdout
    stderr: IO[str] = sys.stderr

    # ---- responses

    def _write(self, message: dict[str, Any]) -> None:
        self.stdout.write(json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n")
        self.stdout.flush()

    def _result(self, rpc_id: Any, result: Any) -> None:
        self._write({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def _error(self, rpc_id: Any, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._write({"jsonrpc": "2.0", "id": rpc_id, "error": error})

    def _diagnostic(
        self, code: int, length: int, id_kind: str, method: str, label: str = ""
    ) -> None:
        # Spec, *Terms*: code, byte length, id kind, method only if implemented, class name
        # or fixed label. Never an id, never a caller string, never message content.
        shown = method if method in METHODS else "unknown"
        tail = f" {label}" if label else ""
        self.stderr.write(
            f"ledgergate serve: {code} bytes={length} id={id_kind} method={shown}{tail}\n"
        )
        self.stderr.flush()

    # ---- one line

    def handle_line(self, line: bytes, oversized: int = 0) -> None:
        """One message. ``oversized`` is the drained byte total of a line the bound refused."""
        length = oversized or len(line)
        if oversized:
            self._diagnostic(PARSE_ERROR, length, "undecoded", "unknown")
            self._error(None, PARSE_ERROR, "line exceeds the transport bound")
            return
        try:
            message = loads(line)
        except (IJsonError, json.JSONDecodeError):
            self._diagnostic(PARSE_ERROR, length, "undecoded", "unknown")
            self._error(None, PARSE_ERROR, "parse error")
            return
        # Shape is judged first (spec, *Terms*): a malformed object is -32600 with or without id.
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or ("id" in message and not _valid_id(message["id"]))
        ):
            rpc_id = message.get("id") if isinstance(message, dict) else None
            echo = rpc_id if _valid_id(rpc_id) else None
            shown = message.get("method") if isinstance(message, dict) else None
            self._diagnostic(
                INVALID_REQUEST, length, _id_kind(message), shown if isinstance(shown, str) else ""
            )
            self._error(echo, INVALID_REQUEST, "invalid request")
            return
        method: str = message["method"]
        is_request = "id" in message
        rpc_id = message.get("id")
        params = message.get("params")

        if not is_request:
            if method == "tools/call":
                # A call that cannot be answered must not run (spec, *Wire decoding*).
                self._diagnostic(INVALID_REQUEST, length, "absent", method, "unanswerable")
            return  # notifications are never answered

        if method == "initialize":
            self._result(
                rpc_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": {"name": "ledgergate", "version": __version__},
                    "capabilities": {"tools": {}},
                },
            )
        elif method == "ping":
            self._result(rpc_id, {})
        elif method == "tools/list":
            from ledgergate.mcp.tools import tool_list

            self._result(rpc_id, {"tools": tool_list()})
        elif method == "tools/call":
            if not isinstance(params, dict):
                self._diagnostic(INVALID_PARAMS, length, _id_kind(message), method)
                self._error(rpc_id, INVALID_PARAMS, "params must be an object")
                return
            self._call(rpc_id, params, length, _id_kind(message))
        else:
            self._diagnostic(METHOD_NOT_FOUND, length, _id_kind(message), method)
            self._error(rpc_id, METHOD_NOT_FOUND, "method not found")

    def _call(self, rpc_id: Any, params: dict[str, Any], length: int, id_kind: str) -> None:
        value = request_for_call(rpc_id, params)
        try:
            response = self.journal.handle(value)
        except JournalError as exc:
            name = type(exc).__name__ if type(exc).__name__ in _KNOWN_CLASSES else "JournalError"
            self._diagnostic(JOURNAL_ERROR, length, id_kind, "tools/call", name)
            self._error(
                rpc_id,
                JOURNAL_ERROR,
                "journal refused the call",
                {"class": name, "message": str(exc)[:_DATA_BOUND]},
            )
            if isinstance(exc, IntegrityError):
                raise SessionEndError(3) from exc
            return
        except Exception as exc:
            # A bug, not a state of the journal: -32603, no data, and the process exits.
            self._diagnostic(INTERNAL_ERROR, length, id_kind, "tools/call", "internal")
            self._error(rpc_id, INTERNAL_ERROR, "internal error")
            raise SessionEndError(4) from exc
        result = response.as_tool_result()
        self._result(
            rpc_id,
            {
                "content": [{"type": "text", "text": canonical_text(result)}],
                "structuredContent": result,
                "isError": not response.ok,
            },
        )

    # ---- the session

    def run(self, stdin: IO[bytes]) -> int:
        """First byte to EOF. Returns the process exit code."""
        try:
            for line, oversized in _lines(stdin):
                self.handle_line(line, oversized)
        except SessionEndError as stop:
            return stop.code
        return 0


def _lines(stdin: IO[bytes]) -> Iterator[tuple[bytes, int]]:
    """Newline-delimited lines with the 16 MiB bound applied *before* decoding, to the content
    excluding its terminator, so the same payload has the same fate with or without a final
    newline. An over-long line yields once as ``(b"", drained_total)`` after its remainder is
    drained to the next newline in bounded chunks (never materialised), so it cannot smuggle
    a second message in its tail; a normal line yields ``(content, 0)``. Blank lines are
    messages too and are refused like any undecodable line."""
    while True:
        line = stdin.readline(MAX_LINE_BYTES + 2)
        if not line:
            return
        content = line.rstrip(b"\r\n") if line.endswith(b"\n") else line
        if len(content) > MAX_LINE_BYTES:
            drained = len(line)
            while not line.endswith(b"\n"):
                line = stdin.readline(MAX_LINE_BYTES)
                if not line:
                    break
                drained += len(line)
            yield b"", drained
            continue
        yield content, 0


def _install_hooks(stderr: IO[str]) -> None:
    """Spec, *Terms*: the stderr vocabulary is closed for the whole process. Both hooks are
    replaced so no exception message is ever printed by the interpreter."""

    def excepthook(*_args: Any) -> None:
        stderr.write("ledgergate serve: internal\n")
        stderr.flush()

    def unraisablehook(*_args: Any) -> None:
        stderr.write("ledgergate serve: internal\n")
        stderr.flush()

    sys.excepthook = excepthook
    sys.unraisablehook = unraisablehook


def serve(
    journal: Journal,
    stdin: IO[bytes] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    """Serve one session and return the exit code. Installs the process-level hooks."""
    err = stderr or sys.stderr
    _install_hooks(err)
    server = Server(journal, stdout or sys.stdout, err)
    try:
        return server.run(stdin or sys.stdin.buffer)
    except BrokenPipeError:
        # The client went away: redirect fd 1 so the interpreter's final flush cannot raise.
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        err.write("ledgergate serve: internal\n")
        return 5
    except Exception:
        err.write("ledgergate serve: internal\n")
        return 4
    finally:
        journal.close()


__all__ = [
    "MAX_LINE_BYTES",
    "PROTOCOL_VERSION",
    "Server",
    "SessionEndError",
    "request_for_call",
    "serve",
]
