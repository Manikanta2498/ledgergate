# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""RFC 7493 I-JSON decoding: the only way untrusted JSON enters admission.

JCS digests are computed over IEEE-754 doubles, so a decoded value must satisfy: every
integer within ``[-(2**53 - 1), 2**53 - 1]``; every double finite; every string a sequence
of Unicode scalar values; no duplicate member names. Python's ``json.loads`` enforces none
of these by default: it keeps the last of duplicate keys silently, parses ``1e400`` to
``inf``, routes ``NaN`` and ``Infinity`` through ``parse_constant`` rather than
``parse_float``, and accepts lone surrogates. Each is handled here, and a violation is a
transport error that never reaches admission and never produces a journal row.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1
MAX_TRANSPORT_DEPTH = 64
MAX_TRANSPORT_NODES = 200_000
"""Transport-class limits: a decoded value deeper or larger than this is refused before any
row is written, like a value that is not I-JSON. Every later stage (JCS, envelopes, the
trace models) then has a bounded input and recursion cannot escape it."""

MAX_TRACE_EVENTS = 5_000_000
"""The trace schema's bound on events; the journal's capacity check keeps every journal under
it (nine events per invocation plus one per message, as an upper bound)."""

MAX_PAYLOAD_DEPTH = 32
MAX_PAYLOAD_NODES = 10_000
"""Payload-class limits, shared by journal admission and the trace models, so anything the
journal admits as tool arguments or serves as a result is representable in a trace."""


class IJsonError(ValueError):
    """The input is JSON but not I-JSON, so it cannot be digested faithfully."""


MAX_INT_LITERAL = 17  # sign included: 2**53 - 1 has 16 digits


def _int(text: str) -> int:
    # Refuse by literal length before int(): Python raises a bare ValueError on literals over
    # 4,300 digits, which would escape the decoder; anything longer than 17 characters is
    # outside the safe range regardless.
    if len(text) > MAX_INT_LITERAL:
        raise IJsonError("integer literal is outside the I-JSON safe range")
    value = int(text)
    if abs(value) > MAX_SAFE_INTEGER:
        raise IJsonError(f"integer {text} is outside the I-JSON safe range")
    return value


def _float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise IJsonError(f"number {text} is not a finite double")
    return value


def _constant(text: str) -> Any:
    raise IJsonError(f"{text} is not a JSON number")


def _pairs(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise IJsonError(f"duplicate member name {k!r}")
        out[k] = v
    return out


def require_ijson(value: Any) -> Any:
    """Validate an already-decoded value (the surrogate rule cannot be a decode hook).
    Returns the value unchanged."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        v, depth = stack.pop()
        nodes += 1
        if nodes > MAX_TRANSPORT_NODES:
            raise IJsonError(f"value exceeds {MAX_TRANSPORT_NODES} nodes")
        if depth > MAX_TRANSPORT_DEPTH:
            raise IJsonError(f"value nesting exceeds {MAX_TRANSPORT_DEPTH}")
        if isinstance(v, str):
            try:
                v.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise IJsonError("string contains an unpaired surrogate") from exc
        elif isinstance(v, dict):
            for k, item in v.items():
                if not isinstance(k, str):
                    raise IJsonError("object keys must be strings")
                stack.append((k, depth + 1))
                stack.append((item, depth + 1))
        elif isinstance(v, list):
            stack.extend((item, depth + 1) for item in v)
        elif isinstance(v, bool) or v is None:
            pass
        elif isinstance(v, int):
            if abs(v) > MAX_SAFE_INTEGER:
                raise IJsonError(f"integer {v} is outside the I-JSON safe range")
        elif isinstance(v, float):
            if not math.isfinite(v):
                raise IJsonError("number is not a finite double")
        else:
            raise IJsonError(f"{type(v).__name__} is not a JSON value")
    return value


def payload_size(value: Any) -> tuple[int, int]:
    """``(nodes, depth)`` of a JSON value, iteratively."""
    nodes, deepest = 0, 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        v, depth = stack.pop()
        nodes += 1
        deepest = max(deepest, depth)
        if isinstance(v, dict):
            stack.extend((item, depth + 1) for item in v.values())
        elif isinstance(v, list):
            stack.extend((item, depth + 1) for item in v)
    return nodes, deepest


def loads(text: str | bytes) -> Any:
    """Decode I-JSON. Raises :class:`IJsonError` for any RFC 7493 violation or transport
    bound and ``json.JSONDecodeError`` for text that is not JSON at all; nothing else escapes,
    whatever the input."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IJsonError("input is not valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            parse_int=_int,
            parse_float=_float,
            parse_constant=_constant,
            object_pairs_hook=_pairs,
        )
    except RecursionError as exc:
        # The C scanner recurses per nesting level and gives up before the depth bound can
        # be counted; that is the depth refusal, not a crash.
        raise IJsonError(f"value nesting exceeds {MAX_TRANSPORT_DEPTH}") from exc
    return require_ijson(value)
