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


class IJsonError(ValueError):
    """The input is JSON but not I-JSON, so it cannot be digested faithfully."""


def _int(text: str) -> int:
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
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            try:
                v.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise IJsonError("string contains an unpaired surrogate") from exc
        elif isinstance(v, dict):
            for k, item in v.items():
                if not isinstance(k, str):
                    raise IJsonError("object keys must be strings")
                stack.append(k)
                stack.append(item)
        elif isinstance(v, list):
            stack.extend(v)
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


def loads(text: str | bytes) -> Any:
    """Decode I-JSON. Raises :class:`IJsonError` for any RFC 7493 violation and
    ``json.JSONDecodeError`` for text that is not JSON at all."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IJsonError("input is not valid UTF-8") from exc
    value = json.loads(
        text,
        parse_int=_int,
        parse_float=_float,
        parse_constant=_constant,
        object_pairs_hook=_pairs,
    )
    return require_ijson(value)
