# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""RFC 8785, JSON Canonicalization Scheme.

Every digest the journal records is SHA-256 over this serialization, so two
implementations must produce identical bytes for identical values. Python's ``json.dumps``
does not: it writes ``5.0`` where JCS writes ``5``, ``1e+16`` where JCS writes
``10000000000000000``, and sorts keys by code point where JCS sorts by UTF-16 code unit.

JCS numbers are IEEE-754 doubles. An ``int`` outside ``[-(2**53 - 1), 2**53 - 1]`` has no
faithful serialization and is refused; callers that need larger integers (Money amounts
are unbounded) encode them as decimal strings before serializing, which is what the
journal specification requires.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

from ledgergate.codec.ijson import MAX_SAFE_INTEGER


class JcsError(ValueError):
    """A value has no RFC 8785 serialization."""


def canonical_text(value: Any) -> str:
    """Serialize ``value`` per RFC 8785. Raises :class:`JcsError` for anything outside
    the JSON data model or the I-JSON numeric domain."""
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def canonical_bytes(value: Any) -> bytes:
    text = canonical_text(value)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:  # lone surrogate
        raise JcsError("string contains an unpaired surrogate") from exc


def digest(value: Any) -> str:
    """Lower-case hex SHA-256 over the canonical bytes."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


# ------------------------------------------------------------------ internals


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise JcsError(f"integer {value} exceeds the JCS-safe range; encode it as a string")
        out.append(str(value))
    elif isinstance(value, float):
        out.append(_es_number(value))
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, Mapping):
        _object(value, out)
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        raise JcsError(f"{type(value).__name__} is not a JSON value")


def _object(value: Mapping[Any, Any], out: list[str]) -> None:
    keys: list[str] = []
    for k in value:
        if not isinstance(k, str):
            raise JcsError("object keys must be strings")
        keys.append(k)
    # RFC 8785 section 3.2.3: sort by UTF-16 code units, not by code point.
    keys.sort(key=lambda k: k.encode("utf-16-be", "surrogatepass"))
    out.append("{")
    for i, k in enumerate(keys):
        if i:
            out.append(",")
        out.append(_string(k))
        out.append(":")
        _write(value[k], out)
    out.append("}")


_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _string(s: str) -> str:
    # RFC 8785 section 3.2.2.2: escape only what JSON requires; leave everything else,
    # including non-ASCII, as literal characters.
    parts = ['"']
    for ch in s:
        if ch in _ESCAPES:
            parts.append(_ESCAPES[ch])
        elif ch < " ":
            parts.append(f"\\u{ord(ch):04x}")
        else:
            parts.append(ch)
    parts.append('"')
    return "".join(parts)


def _es_number(x: float) -> str:
    """ECMAScript ``Number::toString`` for a finite double (RFC 8785 section 3.2.2.3)."""
    if math.isnan(x) or math.isinf(x):
        raise JcsError("NaN and infinities have no JSON serialization")
    if x == 0:
        return "0"  # -0 serializes as 0
    sign = "-" if x < 0 else ""
    x = abs(x)
    # repr() gives the shortest digit string that round-trips, which is exactly the
    # digit sequence ECMAScript specifies; only the layout differs.
    r = repr(x)
    if "e" in r or "E" in r:
        mant, exp = r.lower().split("e")
        e = int(exp)
    else:
        mant, e = r, 0
    if "." in mant:
        ip, fp = mant.split(".")
    else:
        ip, fp = mant, ""
    digits = (ip + fp).lstrip("0")
    # n is the position of the decimal point relative to the digit string: value = 0.digits * 10^n
    n = len(ip.lstrip("0")) + e if ip.lstrip("0") else e - (len(fp) - len(fp.lstrip("0")))
    digits = digits.rstrip("0") or "0"
    k = len(digits)
    if k <= n <= 21:
        body = digits + "0" * (n - k)
    elif 0 < n <= 21:
        body = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        exp_n = n - 1
        es = f"e{'+' if exp_n >= 0 else '-'}{abs(exp_n)}"
        body = digits[0] + ("." + digits[1:] if k > 1 else "") + es
    return sign + body
