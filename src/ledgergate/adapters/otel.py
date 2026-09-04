# SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
# SPDX-License-Identifier: BUSL-1.1
"""The OpenTelemetry GenAI observational adapter, built to ``docs/spec/otel-adapter.md``.

Every rule here is one stated in that document; section names are in the comments. The
adapter is a pure function of the export: the same file yields the same v1 trace, byte for
byte, or the same completeness report. It never invents ledger evidence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from ledgergate.codec import (
    MAX_PAYLOAD_DEPTH,
    MAX_PAYLOAD_NODES,
    MAX_SAFE_INTEGER,
    MAX_TEXT,
    IJsonError,
    IJsonRangeError,
    iter_concatenated,
    loads,
    payload_size,
)
from ledgergate.ledger import InvalidIdentifierError
from ledgergate.ledger.identifiers import require_identifier
from ledgergate.trace.models import Trace

SEMCONV = "1.37"
SCHEMA_URL_SUFFIX = "/1.37.0"
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_NODES = 50_000_000
MAX_DEPTH = 200
MAX_MESSAGE_CHARS = 65_536
MAX_EVENTS = 100_000

INFERENCE_OPERATIONS = frozenset({"chat", "generate_content", "text_completion"})
TOOL_OPERATION = "execute_tool"
AGENT_OPERATION = "invoke_agent"
DETAILS_EVENT = "gen_ai.client.inference.operation.details"
CONTENT_KEYS = ("gen_ai.system_instructions", "gen_ai.input.messages", "gen_ai.output.messages")

_HEX16 = re.compile(r"[0-9a-fA-F]{16}")
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
_NANOS = re.compile(r"[0-9]{1,19}")
_INT = re.compile(r"-?[0-9]{1,16}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

Location = str


class UnreadableError(Exception):
    """Exit 2: the file could not be read or decoded before any OTLP structure existed."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class Finding:
    check: str
    location: Location
    detail: str = ""


@dataclass
class Report:
    """Locations only, never content (spec: *Completeness validation*)."""

    findings: list[Finding]

    def render(self) -> str:
        lines = [f"incomplete: {len(self.findings)} finding(s)"]
        lines.extend(
            f"  {f.check} at {f.location}" + (f": {f.detail}" if f.detail else "")
            for f in self.findings
        )
        return "\n".join(lines)


class Outcome:
    """Either a v1 document (as a JSON-ready dict) or a report; never both."""

    def __init__(self, trace: dict[str, Any] | None, report: Report | None) -> None:
        self.trace = trace
        self.report = report


# ------------------------------------------------------------------ decoding


def read_export(data: bytes) -> list[Any]:
    """Spec *Input*: two framings, decided by the first non-whitespace byte; the whole file is
    decoded before any examination; pre-structure refusals are exit 2 (``UnreadableError``)."""
    if len(data) > MAX_FILE_BYTES:
        raise UnreadableError(f"file exceeds {MAX_FILE_BYTES} bytes")
    stripped = data.lstrip(b" \t\r\n")
    try:
        if stripped[:1] == b"[":
            docs = loads(data, max_nodes=MAX_NODES, max_depth=MAX_DEPTH + 1)
            if not isinstance(docs, list):  # pragma: no cover - `[` always decodes to a list
                raise UnreadableError("not an array")
        else:
            docs = list(iter_concatenated(data, max_nodes=MAX_NODES, max_depth=MAX_DEPTH))
    except IJsonRangeError as exc:
        raise UnreadableError(
            str(exc), hint="OTLP timestamps emitted as JSON numbers are the common cause"
        ) from exc
    except (IJsonError, json.JSONDecodeError) as exc:
        raise UnreadableError(str(exc)) from exc
    if not docs:
        raise UnreadableError("zero documents")
    return docs


# ------------------------------------------------------------------ normalisation


class _Faults:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(self, check: str, location: Location, detail: str = "") -> None:
        self.findings.append(Finding(check, location, detail))


def _any_value(value: Any, where: Location, faults: _Faults) -> tuple[bool, Any]:
    """OTLP ``AnyValue`` -> JSON (spec *OTLP/JSON encoding*). Returns (ok, json)."""
    if not isinstance(value, dict):
        faults.add("shape", where, "value is not an object")
        return False, None
    if len(value) != 1:
        faults.add("shape", where, "value must have exactly one typed member")
        return False, None
    ((kind, inner),) = value.items()
    if kind == "stringValue":
        if not isinstance(inner, str):
            faults.add("shape", where, "stringValue is not a string")
            return False, None
        return True, inner
    if kind == "boolValue":
        if not isinstance(inner, bool):
            faults.add("shape", where, "boolValue is not a boolean")
            return False, None
        return True, inner
    if kind == "doubleValue":
        if isinstance(inner, bool) or not isinstance(inner, int | float):
            faults.add("shape", where, "doubleValue is not a number")
            return False, None
        return True, inner
    if kind == "intValue":
        if not isinstance(inner, str) or not _INT.fullmatch(inner):
            faults.add("shape", where, "intValue is not a bounded decimal string")
            return False, None
        parsed = int(inner)
        if abs(parsed) > MAX_SAFE_INTEGER:
            faults.add("range", where, "intValue outside the I-JSON safe range")
            return False, None
        return True, parsed
    if kind == "arrayValue":
        values = inner.get("values", []) if isinstance(inner, dict) else None
        if not isinstance(values, list):
            faults.add("shape", where, "arrayValue.values is not an array")
            return False, None
        out = []
        for i, item in enumerate(values):
            ok, v = _any_value(item, f"{where}[{i}]", faults)
            if not ok:
                return False, None
            out.append(v)
        return True, out
    if kind == "kvlistValue":
        values = inner.get("values", []) if isinstance(inner, dict) else None
        if not isinstance(values, list):
            faults.add("shape", where, "kvlistValue.values is not an array")
            return False, None
        obj: dict[str, Any] = {}
        for i, kv in enumerate(values):
            if not isinstance(kv, dict) or not isinstance(kv.get("key", ""), str):
                faults.add("shape", f"{where}[{i}]", "kv is not an object with a string key")
                return False, None
            key = kv.get("key", "")
            if key in obj:
                faults.add("shape", f"{where}[{i}]", "repeated key")  # index path, not the key
                return False, None
            ok, v = _any_value(kv.get("value", {}), f"{where}[{i}].value", faults)
            if not ok:
                return False, None
            obj[key] = v
        return True, obj
    if kind == "bytesValue":
        faults.add("shape", where, "bytesValue has no JSON form the trace can carry")
        return False, None
    faults.add("shape", where, "unknown typed member")
    return False, None


def _attributes(
    raw: Any, where: Location, faults: _Faults, wanted: frozenset[str]
) -> dict[str, Any] | None:
    """The read set only: keys are examined for every attribute (a present non-string key is
    a shape fault); values only for wanted keys; one copy per wanted key."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        faults.add("shape", where, "attributes is not an array")
        return None
    out: dict[str, Any] = {}
    seen: set[str] = set()
    for i, kv in enumerate(raw):
        loc = f"{where}[{i}]"
        if not isinstance(kv, dict):
            faults.add("shape", loc, "attribute is not an object")
            return None
        key = kv.get("key", "")
        if not isinstance(key, str):
            faults.add("shape", f"{loc}.key", "key is not a string")
            return None
        if key not in wanted:
            continue
        if key in seen:
            faults.add("one_copy", loc, f"repeated attribute {key}")
            return None
        seen.add(key)
        ok, value = _any_value(kv.get("value", {}), f"{loc}.value", faults)
        if not ok:
            return None
        out[key] = value
    return out


# ------------------------------------------------------------------ spans


@dataclass
class _Span:
    loc: Location
    file_pos: int
    span_id: str
    parent: str
    trace_id: str
    start: int
    end: int
    status: int
    operation: str | None
    attrs: dict[str, Any] = field(default_factory=dict)
    scope_url: str | None = None
    scope_label: str = "absent absent"

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.start, self.end, self.file_pos)


_OPERATION_ONLY = frozenset({"gen_ai.operation.name"})
_READ_SET: dict[str | None, frozenset[str]] = {
    # spec *OTLP/JSON encoding*: exactly the (span class, attribute) pairs the adapter reads
    "inference": frozenset({"gen_ai.operation.name", *CONTENT_KEYS}),
    "inference_error": frozenset({"gen_ai.operation.name", "gen_ai.input.messages"}),
    TOOL_OPERATION: frozenset(
        {
            "gen_ai.operation.name",
            "gen_ai.tool.call.id",
            "gen_ai.tool.name",
            "gen_ai.tool.call.result",
            "error.type",
        }
    ),
    AGENT_OPERATION: frozenset({"gen_ai.operation.name", "gen_ai.agent.name"}),
    None: _OPERATION_ONLY,
}


def _span_class(operation: str | None, status: int) -> str | None:
    if operation in INFERENCE_OPERATIONS:
        return "inference_error" if status == 2 else "inference"
    if operation in (TOOL_OPERATION, AGENT_OPERATION):
        return operation
    return None


def _nanos(value: Any, where: Location, faults: _Faults) -> int | None:
    if not isinstance(value, str) or not _NANOS.fullmatch(value):
        faults.add("timestamp", where, "not a decimal nanosecond string")
        return None
    n = int(value)
    if n == 0:
        faults.add("timestamp", where, "zero")
        return None
    return n


def _collect_spans(
    docs: list[Any], faults: _Faults
) -> tuple[list[_Span], list[str], list[str], str | None]:
    """Walk every document; returns spans in file order, distinct service names, resource
    schema URLs and the GenAI scope URL (or None). Shape faults end the subtree they name."""
    spans: list[_Span] = []
    services: list[str] = []
    resource_urls: list[str] = []
    scope_urls: list[str] = []
    pos = 0
    for d, doc in enumerate(docs):
        dloc = f"[{d}]"
        if not isinstance(doc, dict):
            faults.add("shape", dloc, "document is not an object")
            continue
        rs = doc.get("resourceSpans", [])
        if not isinstance(rs, list):
            faults.add("shape", f"{dloc}.resourceSpans", "not an array")
            continue
        for i, r in enumerate(rs):
            rloc = f"{dloc}.resourceSpans[{i}]"
            if not isinstance(r, dict):
                faults.add("shape", rloc, "not an object")
                continue
            url = r.get("schemaUrl")
            if (
                url is not None
                and _metadata_string(url, f"{rloc}.schemaUrl", faults)
                and url not in resource_urls
            ):
                resource_urls.append(url)
            resource = r.get("resource", {})
            rattrs = (
                _attributes(
                    resource.get("attributes"),
                    f"{rloc}.resource.attributes",
                    faults,
                    frozenset({"service.name"}),
                )
                if isinstance(resource, dict)
                else None
            )
            if resource is not None and not isinstance(resource, dict):
                faults.add("shape", f"{rloc}.resource", "not an object")
            if rattrs and "service.name" in rattrs:
                name = rattrs["service.name"]
                if not isinstance(name, str):
                    faults.add(
                        "type", f"{rloc}.resource.attributes", "service.name is not a string"
                    )
                elif name not in services:
                    services.append(name)
            ss = r.get("scopeSpans", [])
            if not isinstance(ss, list):
                faults.add("shape", f"{rloc}.scopeSpans", "not an array")
                continue
            for j, sc in enumerate(ss):
                sloc = f"{rloc}.scopeSpans[{j}]"
                if not isinstance(sc, dict):
                    faults.add("shape", sloc, "not an object")
                    continue
                scope = sc.get("scope", {})
                label = "absent absent"
                if scope is not None and not isinstance(scope, dict):
                    faults.add("shape", f"{sloc}.scope", "not an object")
                elif isinstance(scope, dict):
                    n, v = scope.get("name", "absent"), scope.get("version", "absent")
                    if _metadata_string(n, f"{sloc}.scope.name", faults) and _metadata_string(
                        v, f"{sloc}.scope.version", faults
                    ):
                        label = f"{n} {v}"
                surl = sc.get("schemaUrl")
                if surl is not None and not _metadata_string(surl, f"{sloc}.schemaUrl", faults):
                    surl = None
                raw_spans = sc.get("spans", [])
                if not isinstance(raw_spans, list):
                    faults.add("shape", f"{sloc}.spans", "not an array")
                    continue
                genai_scope = False
                for k, s in enumerate(raw_spans):
                    loc = f"{sloc}.spans[{k}]"
                    pos += 1
                    span = _span(s, loc, pos, faults)
                    if span is None:
                        continue
                    if span.operation is not None:
                        genai_scope = True
                    span.scope_url = surl
                    span.scope_label = label
                    spans.append(span)
                if genai_scope and surl is not None and surl not in scope_urls:
                    scope_urls.append(surl)
    scope_url: str | None = None
    if len(scope_urls) > 1:
        faults.add("convention", "scopeSpans", "GenAI scopes carry differing schemaUrls")
    elif scope_urls:
        scope_url = scope_urls[0]
        if not scope_url.endswith(SCHEMA_URL_SUFFIX):
            faults.add(
                "convention", "scopeSpans", f"schemaUrl is not {SEMCONV}: {scope_url[:MAX_TEXT]}"
            )
    return spans, services, resource_urls, scope_url


def _metadata_string(value: Any, where: Location, faults: _Faults) -> bool:
    if not isinstance(value, str):
        faults.add("shape", where, "not a string")
        return False
    if _CONTROL.search(value) or len(value) > MAX_TEXT:
        faults.add("metadata", where, "line break, control character or over 1024 characters")
        return False
    return True


def _span(s: Any, loc: Location, pos: int, faults: _Faults) -> _Span | None:
    if not isinstance(s, dict):
        faults.add("shape", loc, "span is not an object")
        return None
    span_id = s.get("spanId")
    if not isinstance(span_id, str) or not _HEX16.fullmatch(span_id):
        faults.add("span_id", loc, "missing or not 16 hex characters")
        return None
    parent = s.get("parentSpanId", "")
    if not isinstance(parent, str) or (parent and not _HEX16.fullmatch(parent)):
        faults.add("span_id", f"{loc}.parentSpanId", "not 16 hex characters")
        return None
    trace_id = s.get("traceId")
    if not isinstance(trace_id, str) or not _HEX32.fullmatch(trace_id):
        faults.add("trace_id", loc, "missing or not 32 hex characters")
        return None
    start = _nanos(s.get("startTimeUnixNano"), f"{loc}.startTimeUnixNano", faults)
    end = _nanos(s.get("endTimeUnixNano"), f"{loc}.endTimeUnixNano", faults)
    if start is None or end is None:
        return None
    if end < start:
        faults.add("timestamp", loc, "end before start")
        return None
    status = s.get("status", {})
    code: Any = 0
    if status is not None:
        if not isinstance(status, dict):
            faults.add("shape", f"{loc}.status", "not an object")
            return None
        code = status.get("code", 0)
        if isinstance(code, bool) or not isinstance(code, int) or code not in (0, 1, 2):
            faults.add("shape", f"{loc}.status.code", "not 0, 1 or 2")
            return None
    # pass 1: the operation name decides the span class and therefore the read set
    first = _attributes(s.get("attributes"), f"{loc}.attributes", faults, _OPERATION_ONLY)
    if first is None:
        return None
    operation = first.get("gen_ai.operation.name")
    if operation is not None and not isinstance(operation, str):
        faults.add("type", f"{loc}.attributes", "gen_ai.operation.name is not a string")
        return None
    klass = _span_class(operation, code)
    attrs = _attributes(s.get("attributes"), f"{loc}.attributes", faults, _READ_SET[klass])
    if attrs is None:
        return None
    # the event form of content, inference spans only: one copy per key across forms
    events = s.get("events", [])
    if not isinstance(events, list):
        faults.add("shape", f"{loc}.events", "not an array")
        return None
    content_keys = _READ_SET[klass] & frozenset(CONTENT_KEYS)
    details = 0
    for i, ev in enumerate(events):
        eloc = f"{loc}.events[{i}]"
        if not isinstance(ev, dict):
            faults.add("shape", eloc, "event is not an object")
            return None
        if ev.get("name") != DETAILS_EVENT or not content_keys:
            continue
        details += 1
        if details > 1:
            faults.add("one_copy", eloc, "more than one details event")
            return None
        eattrs = _attributes(ev.get("attributes"), f"{eloc}.attributes", faults, content_keys)
        if eattrs is None:
            return None
        for key, value in eattrs.items():
            if key in attrs:
                faults.add("one_copy", eloc, f"{key} present as attribute and event")
                return None
            attrs[key] = value
    span = _Span(
        loc,
        pos,
        span_id.lower(),
        parent.lower(),
        trace_id.lower(),
        start,
        end,
        code,
        operation,
        attrs,
    )
    # status.message for tool spans
    if isinstance(status, dict):
        msg = status.get("message", "")
        if not isinstance(msg, str):
            faults.add("shape", f"{loc}.status.message", "not a string")
            return None
        span.attrs["__status_message"] = msg
    return span


# ------------------------------------------------------------------ content


def _content(value: Any, where: Location, faults: _Faults) -> list[Any] | None:
    """A content attribute: a JSON string (parsed under the adapter's bounds, message
    withheld) or a native structure; the result is a list of messages."""
    if isinstance(value, str):
        try:
            value = loads(value, max_nodes=MAX_NODES, max_depth=MAX_DEPTH)
        except (IJsonError, json.JSONDecodeError):
            faults.add("content", where, "string does not decode")  # decoder message withheld
            return None
    if not isinstance(value, list):
        faults.add("content", where, "not an array of messages")
        return None
    for m, msg in enumerate(value):
        if not isinstance(msg, dict) or not isinstance(msg.get("parts", []), list):
            faults.add("content", f"{where}[{m}]", "message is not an object with parts")
            return None
        for p, part in enumerate(msg.get("parts", [])):
            if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                faults.add("content", f"{where}[{m}].parts[{p}]", "part without a string type")
                return None
    return value


def _instructions(value: Any, where: Location, faults: _Faults) -> list[Any] | None:
    """system_instructions: an array of parts (or a JSON string of one)."""
    if isinstance(value, str):
        try:
            value = loads(value, max_nodes=MAX_NODES, max_depth=MAX_DEPTH)
        except (IJsonError, json.JSONDecodeError):
            faults.add("content", where, "string does not decode")
            return None
    if not isinstance(value, list):
        faults.add("content", where, "not an array of parts")
        return None
    for p, part in enumerate(value):
        if not isinstance(part, dict) or not isinstance(part.get("type"), str):
            faults.add("content", f"{where}[{p}]", "part without a string type")
            return None
    return value


# ------------------------------------------------------------------ mapping


@dataclass
class _Event:
    """A produced event with its full ordering key (spec step 4)."""

    ns: int
    span: _Span
    rank: int
    message: int
    part: int
    step: int
    body: dict[str, Any]
    r: int = 0

    @property
    def key(self) -> tuple[int, int, int, int, int, int, int, int, int]:
        return (self.ns, self.r, *self.span.key, self.rank, self.message, self.part, self.step)


def _render(ns: int) -> str:
    # truncation to the microsecond, never rounding
    return (
        datetime.fromtimestamp(ns // 1_000_000_000, UTC)
        .replace(microsecond=(ns % 1_000_000_000) // 1000)
        .isoformat()
    )


def _ident(value: Any) -> bool:
    try:
        require_identifier(value, "value")
    except InvalidIdentifierError:
        return False
    return True


def convert(data: bytes) -> Outcome:
    """Bytes of an export -> a v1 document or a report. Raises ``UnreadableError`` for exit 2."""
    docs = read_export(data)
    faults = _Faults()
    spans, services, resource_urls, scope_url = _collect_spans(docs, faults)
    if not spans:
        faults.add("spans", "[0]", "no spans")
    by_id: dict[str, _Span] = {}
    for sp in spans:
        if sp.span_id in by_id:
            faults.add("span_id", sp.loc, "duplicate spanId")
        by_id[sp.span_id] = sp
    trace_ids = {sp.trace_id for sp in spans}
    if len(trace_ids) > 1:
        faults.add("trace_id", "spans", "more than one traceId")
    for sp in spans:
        if sp.parent and sp.parent not in by_id:
            faults.add("parent", sp.loc, "parentSpanId names no span in the document")
    if len(services) > 1:
        faults.add("service", "resourceSpans", "differing service.name values")

    inference = sorted(
        (s for s in spans if s.operation in INFERENCE_OPERATIONS), key=lambda s: s.key
    )
    tools = [s for s in spans if s.operation == TOOL_OPERATION]
    agents = [s for s in spans if s.operation == AGENT_OPERATION]

    events: list[_Event] = []
    emitted: list[tuple[str, str, int]] = []  # (role, text, emitted-at ns)
    calls: dict[str, tuple[_Span, _Event, str]] = {}  # call_id -> (emitter, event, name)
    responses: list[tuple[_Span, int, int, str, Any]] = []  # (span, m, p, id, response)

    for s in inference:
        loc = s.loc
        inputs = None
        if "gen_ai.input.messages" in s.attrs:
            inputs = _content(
                s.attrs["gen_ai.input.messages"], f"{loc}.gen_ai.input.messages", faults
            )
        elif s.status != 2:
            faults.add("input_messages", loc, "inference span without gen_ai.input.messages")
        # response parts are a source on every inference span, error ones included
        if inputs is not None:
            for m, msg in enumerate(inputs):
                for p, part in enumerate(msg.get("parts", [])):
                    if part.get("type") == "tool_call_response":
                        if not _ident(part.get("id")):
                            faults.add(
                                "content",
                                f"{loc}.gen_ai.input.messages[{m}].parts[{p}]",
                                "response part without an identifier id",
                            )
                            continue
                        if "response" not in part:
                            faults.add(
                                "content",
                                f"{loc}.gen_ai.input.messages[{m}].parts[{p}]",
                                "response part without response",
                            )
                            continue
                        responses.append((s, m, p, part["id"], part["response"]))
        if s.status == 2:
            continue
        # step 1: presented conversation and the prefix rule
        presented: list[tuple[str, str, int, int, int]] = []  # role, text, rank, m, p
        if "gen_ai.system_instructions" in s.attrs:
            parts = _instructions(
                s.attrs["gen_ai.system_instructions"], f"{loc}.gen_ai.system_instructions", faults
            )
            for p, part in enumerate(parts or []):
                if part.get("type") == "text":
                    if not isinstance(part.get("content"), str):
                        faults.add(
                            "content",
                            f"{loc}.gen_ai.system_instructions[{p}]",
                            "text part without string content",
                        )
                        continue
                    presented.append(("system", part["content"], 0, 0, p))
        if inputs is not None:
            for m, msg in enumerate(inputs):
                role = msg.get("role")
                if role not in ("system", "user", "assistant", "tool"):
                    faults.add("role", f"{loc}.gen_ai.input.messages[{m}]", "role outside v1's set")
                    continue
                for p, part in enumerate(msg.get("parts", [])):
                    if part.get("type") == "text":
                        if not isinstance(part.get("content"), str):
                            faults.add(
                                "content",
                                f"{loc}.gen_ai.input.messages[{m}].parts[{p}]",
                                "text part without string content",
                            )
                            continue
                        presented.append((role, part["content"], 1, m, p))
        # prefix rule
        prefix_ok = True
        for i, (role, text, at_ns) in enumerate(emitted):
            if i >= len(presented) or (presented[i][0], presented[i][1]) != (role, text):
                faults.add("prefix", loc, f"presented conversation diverges at position {i}")
                prefix_ok = False
                break
            if at_ns > s.start:
                faults.add("time", loc, f"presented item {i} was emitted after this span started")
        if prefix_ok:
            for role, text, rank, m, p in presented[len(emitted) :]:
                if len(text) > MAX_MESSAGE_CHARS:
                    faults.add(
                        "bound",
                        f"{loc}.gen_ai.input.messages[{m}].parts[{p}]",
                        "message over 65536 characters",
                    )
                    continue
                events.append(
                    _Event(
                        s.start,
                        s,
                        rank,
                        m,
                        p,
                        0,
                        {"type": "message", "role": role, "content": text},
                    )
                )
                emitted.append((role, text, s.start))
        # outputs
        if "gen_ai.output.messages" not in s.attrs:
            faults.add("output_messages", loc, "inference span without gen_ai.output.messages")
            continue
        outputs = _content(
            s.attrs["gen_ai.output.messages"], f"{loc}.gen_ai.output.messages", faults
        )
        if outputs is None:
            continue
        for m, msg in enumerate(outputs):
            for p, part in enumerate(msg.get("parts", [])):
                ploc = f"{loc}.gen_ai.output.messages[{m}].parts[{p}]"
                kind = part.get("type")
                if kind == "text":
                    if not isinstance(part.get("content"), str):
                        faults.add("content", ploc, "text part without string content")
                        continue
                    text = part["content"]
                    if len(text) > MAX_MESSAGE_CHARS:
                        faults.add("bound", ploc, "message over 65536 characters")
                        continue
                    events.append(
                        _Event(
                            s.end,
                            s,
                            2,
                            m,
                            p,
                            0,
                            {"type": "message", "role": "assistant", "content": text},
                        )
                    )
                    emitted.append(("assistant", text, s.end))
                elif kind == "tool_call":
                    call_id, name = part.get("id"), part.get("name")
                    if not _ident(call_id) or not _ident(name):
                        faults.add("tool_call", ploc, "id or name is not an identifier")
                        continue
                    args = part.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = loads(args, max_nodes=MAX_NODES, max_depth=MAX_DEPTH)
                        except (IJsonError, json.JSONDecodeError):
                            faults.add("tool_call", ploc, "arguments string does not decode")
                            continue
                    if not isinstance(args, dict):
                        faults.add("tool_call", ploc, "arguments is not an object")
                        continue
                    nodes, depth = payload_size(args)
                    if nodes > MAX_PAYLOAD_NODES or depth > MAX_PAYLOAD_DEPTH:
                        faults.add("bound", ploc, "arguments exceed the payload bound")
                        continue
                    if call_id in calls:
                        faults.add("tool_call", ploc, "duplicate call id")
                        continue
                    body: dict[str, Any] = {
                        "type": "tool_call",
                        "call_id": call_id,
                        "tool": name,
                        "arguments": args,
                    }
                    key = args.get("idempotency_key")
                    if _ident(key):
                        body["idempotency_key"] = key
                    ev = _Event(s.end, s, 2, m, p, 1, body)
                    events.append(ev)
                    calls[call_id] = (s, ev, name)

    # step 3: results
    by_call_tool: dict[str, _Span] = {}
    for t in tools:
        cid = t.attrs.get("gen_ai.tool.call.id")
        if cid is None:
            faults.add("tool_span", t.loc, "execute_tool without gen_ai.tool.call.id")
            continue
        if not isinstance(cid, str):
            faults.add("type", t.loc, "gen_ai.tool.call.id is not a string")
            continue
        if cid not in calls:
            faults.add("orphan", t.loc, "execute_tool for no observed tool_call")
            continue
        if cid in by_call_tool:
            faults.add("tool_span", t.loc, "second execute_tool for one call id")
            continue
        by_call_tool[cid] = t
        tname = t.attrs.get("gen_ai.tool.name")
        if tname is not None and (not isinstance(tname, str) or tname != calls[cid][2]):
            faults.add("tool_name", t.loc, "gen_ai.tool.name differs from the call's name")
    response_for: dict[str, tuple[_Span, int, int, Any]] = {}
    for s, m, p, cid, resp in sorted(responses, key=lambda r: (r[0].key, r[1], r[2])):
        ploc = f"{s.loc}.gen_ai.input.messages[{m}].parts[{p}]"
        if cid not in calls:
            faults.add("orphan", ploc, "tool_call_response for no observed tool_call")
            continue
        emitter = calls[cid][0]
        if s is emitter or s.start < emitter.start:
            faults.add(
                "response_before_call",
                ploc,
                "response presented no later than its call was emitted",
            )
            continue
        response_for.setdefault(cid, (s, m, p, resp))
    for cid, (emitter, call_ev, _name) in calls.items():
        if cid in by_call_tool:
            t = by_call_tool[cid]
            ok = t.status != 2
            result_body: dict[str, Any] = {"type": "tool_result", "call_id": cid, "ok": ok}
            if not ok:
                etype = t.attrs.get("error.type")
                if etype is not None and not isinstance(etype, str):
                    faults.add("type", t.loc, "error.type is not a string")
                    continue
                if etype is None:
                    etype = "otel.status_error"
                emsg = t.attrs.get("__status_message", "")
                if not (1 <= len(etype) <= 256) or len(emsg) > MAX_TEXT:
                    faults.add("bound", t.loc, "error.type or status message out of bounds")
                    continue
                result_body["error"] = {"type": etype, "message": emsg}
            if "gen_ai.tool.call.result" in t.attrs:
                result_body["result"] = t.attrs["gen_ai.tool.call.result"]
            ev = _Event(t.end, t, 0, 0, 0, 2, result_body)
        elif cid in response_for:
            s, m, p, resp = response_for[cid]
            result_body = {"type": "tool_result", "call_id": cid, "ok": True, "result": resp}
            ev = _Event(s.start, s, 1, m, p, 2, result_body)
        else:
            faults.add(
                "result",
                f"{call_ev.span.loc}.gen_ai.output.messages[{call_ev.message}].parts[{call_ev.part}]",
                "no result observed for this call",
            )
            continue
        if "result" in ev.body and ev.body["result"] is not None:
            nodes, depth = payload_size(ev.body["result"])
            if nodes > MAX_PAYLOAD_NODES or depth > MAX_PAYLOAD_DEPTH:
                faults.add("bound", ev.span.loc, "result exceeds the payload bound")
                continue
        if (
            ev.ns == call_ev.ns
            and (ev.span.start, ev.span.end) == (emitter.start, emitter.end)
            and ev.span is not emitter
        ):
            ev.r = 1
        events.append(ev)

    # step 4: ordering, pairing check
    events.sort(key=lambda e: e.key)
    seq_of_call = {
        e.body["call_id"]: i for i, e in enumerate(events) if e.body["type"] == "tool_call"
    }
    for i, e in enumerate(events):
        if e.body["type"] == "tool_result" and seq_of_call.get(e.body["call_id"], -1) > i:
            faults.add("pairing", e.span.loc, "tool_result ordered before its tool_call")
    if not events:
        faults.add("events", "[0]", "the mapping produced no events")
    if len(events) > MAX_EVENTS:
        faults.add("bound", "events", f"more than {MAX_EVENTS} events")

    # step 5: top level
    agent_name = "unknown"
    for a in agents:
        name = a.attrs.get("gen_ai.agent.name")
        if name is not None and not isinstance(name, str):
            faults.add("type", a.loc, "gen_ai.agent.name is not a string")
    agent_names = sorted(
        {
            a.attrs["gen_ai.agent.name"]
            for a in agents
            if isinstance(a.attrs.get("gen_ai.agent.name"), str)
        }
    )
    # root-most: the earliest-starting parentless one, ties by file position (spec step 5)
    by_start = lambda a: (a.start, a.file_pos)  # noqa: E731
    roots = sorted((a for a in agents if not a.parent), key=by_start) or sorted(
        agents, key=by_start
    )
    if roots and isinstance(roots[0].attrs.get("gen_ai.agent.name"), str):
        agent_name = roots[0].attrs["gen_ai.agent.name"]
    elif services:
        agent_name = services[0]
    if not _ident(agent_name):
        faults.add("agent", "resourceSpans", "agent name is not an identifier")
    metadata = {
        "otel.semconv": SEMCONV,
        "otel.scope_schema_url": scope_url or "absent",
        "otel.resource_schema_urls": ";".join(sorted(resource_urls)) or "absent",
        "otel.scope": ";".join(sorted({s.scope_label for s in spans if s.operation is not None}))
        or "absent",
        "otel.spans": str(len(spans)),
        "otel.agents": str(len(agent_names)),
    }
    for key, value in metadata.items():
        if len(value) > MAX_TEXT:
            faults.add("metadata", f"metadata.{key}", "joined value over 1024 characters")

    if faults.findings:
        return Outcome(None, Report(faults.findings))
    doc = {
        "schema_version": "1",
        "trace_id": next(iter(trace_ids)),
        "agent": {"name": agent_name},
        "started_at": _render(min(s.start for s in spans)),
        "ended_at": _render(max(s.end for s in spans)),
        "metadata": metadata,
        "events": [{"seq": i + 1, "at": _render(e.ns), **e.body} for i, e in enumerate(events)],
    }
    return Outcome(doc, None)


class SelfCheckError(Exception):
    """Exit 70: the produced document does not load; a bug, reported without content."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def self_check(doc: dict[str, Any]) -> Trace:
    try:
        return Trace.model_validate(doc)
    except ValidationError as exc:
        raise SelfCheckError(
            [f"{e['type']}: {e['msg']}" for e in exc.errors(include_input=False)]
        ) from exc


__all__ = [
    "MAX_DEPTH",
    "MAX_FILE_BYTES",
    "MAX_NODES",
    "SEMCONV",
    "Finding",
    "Outcome",
    "Report",
    "SelfCheckError",
    "UnreadableError",
    "convert",
    "read_export",
    "self_check",
]
