# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""Reading, writing and contract-checking traces.

Two validators exist on purpose. :func:`load_trace` uses the typed models and is what the
runtime calls. :func:`validate_document` checks a raw document against the published JSON
Schema, which is the contract a consumer on another stack sees. They must agree, and
``tests/contract`` holds them to it.

The schema file lives at the repository root, outside the wheel (see ADR-0001), so it
must be located explicitly. :func:`default_schema_path` finds it when running from a
checkout and raises otherwise, rather than silently validating against nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from ledgergate.trace.models import Trace
from ledgergate.trace.v2 import TraceV2, lift

SCHEMA_RELATIVE = Path("schema") / "trace" / "v1.json"


class TraceError(ValueError):
    """A document is not a valid trace. ``problems`` lists every reason found."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        head = problems[0] if problems else "invalid trace"
        more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
        super().__init__(f"{head}{more}")


class SchemaNotFoundError(FileNotFoundError):
    pass


def default_schema_path(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default: this file) to find ``schema/trace/v1.json``."""
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        path = candidate / SCHEMA_RELATIVE
        if path.is_file():
            return path
    raise SchemaNotFoundError(
        f"{SCHEMA_RELATIVE} not found above {here}; the schema ships separately from the"
        " runtime, pass its path explicitly"
    )


def load_schema(path: Path | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads((path or default_schema_path()).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def iter_schema_problems(document: Any, schema_path: Path | None = None) -> Iterator[str]:
    """Every way ``document`` fails the JSON Schema, as JSON-pointer-prefixed messages."""
    validator = Draft202012Validator(load_schema(schema_path), format_checker=FormatChecker())
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        pointer = "/" + "/".join(str(p) for p in error.absolute_path)
        yield f"{pointer}: {error.message}"


def validate_document(document: Any, schema_path: Path | None = None) -> None:
    """Raise :class:`TraceError` if ``document`` does not satisfy the published schema."""
    problems = list(iter_schema_problems(document, schema_path))
    if problems:
        raise TraceError(problems)


def parse_trace(document: Any) -> Trace:
    """Typed parse of an already-decoded document. Raises :class:`TraceError`."""
    try:
        return Trace.model_validate(document)
    except ValidationError as exc:
        raise TraceError(
            [f"/{'/'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        ) from None


def load_trace(source: str | bytes | Path) -> Trace:
    """Parse a trace from JSON text, bytes, or a file path."""
    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
    try:
        return Trace.model_validate_json(text)
    except ValidationError as exc:
        raise TraceError(
            [f"/{'/'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        ) from None


def load_any(source: str | bytes | Path) -> TraceV2:
    """Load a v1 or v2 document as v2: a v1 document is lifted under the legacy grammar."""
    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise TraceError(["/: not UTF-8"]) from None
    try:
        version = json.loads(text).get("schema_version")
    except (ValueError, AttributeError, RecursionError) as exc:
        # RecursionError: the stdlib scanner gives up on deep nesting before pydantic sees it
        raise TraceError([f"/: not a JSON object: {type(exc).__name__}"]) from None
    if version == "1":
        return lift(load_trace(text))
    if version != "2":
        raise TraceError([f"/schema_version: expected '1' or '2', got {version!r}"])
    try:
        return TraceV2.model_validate_json(text)
    except ValidationError as exc:
        raise TraceError(
            [f"/{'/'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        ) from None


def dump_v2(trace: TraceV2) -> str:
    """Canonical text form, as :func:`dump_trace` is for v1: sorted keys, two-space indent,
    trailing newline, no NaN."""
    doc = trace.model_dump(mode="json", exclude_none=True)
    return json.dumps(doc, indent=2, sort_keys=True, allow_nan=False) + "\n"


def dump_trace(trace: Trace) -> str:
    """Canonical JSON: sorted keys, two-space indent, trailing newline, no NaN, UTF-8.

    Two equal traces always produce byte-identical text, which is what makes a recorded
    cassette diffable and a replay comparable.
    """
    document = trace.model_dump(mode="json", exclude_none=True)
    return (
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    )


def write_trace(trace: Trace, path: Path) -> None:
    path.write_text(dump_trace(trace), encoding="utf-8")
