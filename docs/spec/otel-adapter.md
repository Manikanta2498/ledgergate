<!--
SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
SPDX-License-Identifier: Apache-2.0
-->

# The OpenTelemetry GenAI observational adapter (M5)

ADR-0002 §6: the journal is authoritative; an OpenTelemetry adapter maps `gen_ai.*` spans to
trace events, validates completeness against the contract, and yields either a conforming
trace or a report of exactly what was missing. This document is the contract that adapter is
built to. It is *observational*: it describes what an agent said and called as some other
system recorded it. It never invents ledger evidence, never runs a command, and never claims
what only the journal can prove.

## What the adapter produces, and what it does not

- **Output**: a schema **v1** trace (`schema/trace/v1.json`) of `message`, `tool_call` and
  `tool_result` events, or a **completeness report**, never both. A v1 document is the right
  target: v1 is the offline ingest format, and everything the adapter can know is expressible
  in it. Loaded through `load_any`, the document lifts to v2 under the `legacy` grammar with
  no intents, so `ledgergate verify` reports every ledger and policy invariant as
  `no_evidence`: an observational trace is evidence of *conversation*, not of *books*, and
  the tooling says so rather than passing it.
- **Never produced**: `ledger_command` or `ledger_result` events. An OTel export of an agent
  talking to `ledgergate serve` shows tool calls named `post`, `refund` and so on; whether
  they reached a ledger, and what the ledger did, is the journal's evidence, derived by
  `ledgergate.derive`, and the adapter does not guess at it from span attributes. Joining an
  observational trace to a journal-derived one (by `call_id`) is future work, and nothing
  here claims it.

## Input

One OTLP/JSON document (`ExportTraceServiceRequest`: `resourceSpans[].scopeSpans[].spans[]`)
as written by the OpenTelemetry file exporter or a collector's file exporter, or a JSON array
of such documents (one per export batch). It is decoded by the project's I-JSON decoder
before anything else looks at it, with **adapter-specific bounds**: the transport bounds
(200,000 nodes) would refuse a real export at about a thousand spans (an OTLP `KeyValue` is
about seven nodes), so `codec.loads` gains explicit `max_nodes`/`max_depth` parameters, the
adapter calls it with 50,000,000 nodes and depth 200, and the file itself is refused before
decoding if it exceeds 256 MiB (read with a limit). The depth bound is 200, not the
transport's 64: in OTLP/JSON the *native* (span-event) form of a content attribute reaches
nesting about 30 before the first `arguments` member and each further JSON object level costs
four more (`value -> kvlistValue -> values -> kv`), so 64 would admit about eight levels of
arguments natively against the 32 the string form allows; 64 + 4 * 32 admits both forms
alike (`require_ijson` is iterative and the scanner's recursion budget is far above 200). A
file within the byte bound but over the node or depth bound is reported (exit `1`,
"document exceeds N nodes"), not tracebacked. The payload
bounds of the *produced* events are the v1 model's and are checked per event (below). Anything
else (protobuf, OTLP/HTTP framing, a live receiver) is out of scope: the adapter reads a file.

**OTLP/JSON encoding.** Attribute values arrive typed (`stringValue`, `boolValue`,
`intValue` and the nano timestamp fields as *decimal strings*, `doubleValue`, `arrayValue`,
`kvlistValue`, `bytesValue`); `status.code` is an integer (`0` unset, `1` ok, `2` error). The
nano timestamp fields must match `^[0-9]{1,20}$` and render to a `datetime` (at most
year 9999, about 2.5e20); anything else is a completeness fault naming the span. A producer
that emits them as JSON *numbers* is refused at decode (exit `2`), since 1.7e18 exceeds the
I-JSON safe integer and the decoder cannot represent it; the report says so by name, because
this is the one common producer deviation with an unhelpful default message.
The adapter normalises an `AnyValue` to JSON before any mapping: strings, booleans and
doubles as themselves; `intValue` parsed, and a value outside the I-JSON safe range is a
fault naming the span and key; `arrayValue` to a JSON array; `kvlistValue` to a JSON object
(a repeated key is a fault, located by index path below the top-level attribute, since keys
inside `arguments` are agent content); `bytesValue` is a fault (the conventions carry no bytes
the adapter reads). Content attributes may be a JSON *string* (the attribute form) or a native
structure (the event form); a string is parsed with the same decoder and the two are then one
shape.

Span fields used: `traceId`, `spanId`, `parentSpanId`, `name`, `startTimeUnixNano`,
`endTimeUnixNano`, `attributes[]` (`key`, `value` in OTLP's typed encoding: `stringValue`,
`intValue`, `boolValue`, `doubleValue`, `arrayValue`, `kvlistValue`), `events[]`
(`timeUnixNano`, `name`, `attributes[]`), `status`. Resource and scope attributes are read
for `service.name` (the agent's name) and the instrumentation scope's version.

## Semantic conventions targeted

OpenTelemetry Semantic Conventions for Generative AI, **version 1.37** (status:
development). The adapter reads:

| Convention | Used for |
| :-- | :-- |
| `gen_ai.operation.name` on a span: `chat`, `generate_content`, `text_completion` | an **inference span**: its output messages become `message` events (role `assistant`) and its tool-call parts become `tool_call` events |
| `gen_ai.operation.name` = `invoke_agent` | a **structural span**: the parent of inference and tool spans. It produces no events and its content attributes, if any, are ignored (they repeat its children's), so a tool call is never emitted twice; it contributes only `gen_ai.agent.name` (preferred over `service.name` for `agent.name`) and its time bounds |
| `gen_ai.operation.name` = `execute_tool` | a tool execution span: `gen_ai.tool.call.id`, `gen_ai.tool.name`; its result is the `tool_result` |
| `gen_ai.input.messages`, `gen_ai.output.messages` (opt-in content attributes, JSON text) | message content, tool-call parts (`type: tool_call`, `id`, `name`, `arguments`) and tool-response parts (`type: tool_call_response`, `id`, `response`) |
| `gen_ai.system_instructions` | a `message` with role `system` |
| span events named `gen_ai.client.inference.operation.details` | the same content when the instrumentation emits it as an event rather than an attribute |
| `error.type`, span `status` | a failed `tool_result` (`ok: false`, `error.type` as the type) |

The mapping is versioned by that convention version. An export carries no convention
version except the optional `schemaUrl` on `resourceSpans[]`/`scopeSpans[]`, which most GenAI
instrumentations leave empty; the instrumentation scope's version is the library's release,
not the convention's. So: when a `schemaUrl` is present and does not end in `/1.37.0`, the
adapter refuses with a report naming the URL found; when absent, the adapter *assumes* 1.37
and records that assumption. `metadata` carries `otel.semconv: "1.37"`, `otel.schema_url`
(the URL, or `absent`) and `otel.scope: <instrumentation scope name and its library
version>`. A later convention is a new mapping, reviewed against this document; the adapter
does not guess at attributes it was not written for. Spans without `gen_ai.operation.name`,
or with one this document does not map (`embeddings`, `create_agent`, ...), produce no events;
they are counted in `otel.spans` and take part in the parent check.

## The mapping

Every produced event carries `at` from a span or span-event timestamp (nanoseconds since
epoch, rendered as a UTC `Timestamp`) and `seq` from the ordering below.

1. **Messages.** Inference spans are processed in order of `startTimeUnixNano`, ties by
   position in the file. The adapter keeps the *emitted conversation*: a sequence of
   `(role, text)` items. For each span it builds the span's *presented conversation*: each
   text part of `gen_ai.system_instructions` as `(system, text)`, then, for each message in
   `gen_ai.input.messages` (a message has a `role`; its `parts` have a `type`), each `text`
   part as `(role, text)`. The emitted conversation must be a positional prefix of the
   presented one; the suffix is emitted as `message` events at the span's start time and
   appended to the emitted conversation. If it is not a prefix (the history the agent was
   shown diverged from what was emitted: an edited, dropped or reordered turn) that is a
   completeness fault naming the span and the first differing position. This is a prefix
   rule, not a set rule: a genuine repeated turn (`user "yes"` twice) is two messages,
   because it lengthens the presented sequence. A known consequence: context-window
   trimming, where an agent framework drops early turns before the next inference, presents
   a *shorter* history and is reported as a fault; the adapter cannot tell trimming from
   editing, and says so rather than guessing. A message `role` outside v1's set (`system`,
   `user`, `assistant`, `tool`) is a located fault. The invariant: after the last inference
   span whose status is not error, the emitted conversation equals that span's presented
   conversation followed by its output. Then each `text` part of `gen_ai.output.messages`
   becomes a `message` (role `assistant`) at the span's end time and is appended. An
   inference span whose status is error (`status.code` 2) produces no events and is skipped
   entirely, prefix check included: a failed inference is not conversation, and its
   presented history may legitimately be the one the next successful span re-presents.
2. **Tool calls.** Each `tool_call` part in an inference span's `gen_ai.output.messages`
   becomes a `tool_call` event at the span's end time with `call_id` = the part's `id`,
   `tool` = the part's `name`, `arguments` = the part's `arguments` (an object; a string is
   parsed as JSON, and a string that does not parse as an object is a completeness fault).
   `idempotency_key` is set from `arguments.idempotency_key` when that member is a string
   satisfying the identifier grammar (the `ledgergate serve` convention) and omitted
   otherwise, never a fault; the member is left in the arguments too, since the adapter does
   not rewrite what the agent sent.
3. **Tool results.** For each `tool_call`, exactly one `tool_result` is *selected*, from two
   possible sources, in this preference: an `execute_tool` span whose `gen_ai.tool.call.id`
   matches (`ok` false iff `status.code` is 2; then `error.type` = the `error.type` attribute,
   or the fixed label `otel.status_error` when the instrumentation set none, and
   `error.message` = the status message, or the empty string when OTLP carries none (v1
   requires the field), a fault if over 1,024 characters; `result`
   = `gen_ai.tool.call.result` if captured, at the span's end time); else the first
   `tool_call_response` part, in processing order, in a later inference span's
   `gen_ai.input.messages` whose `id` matches (`ok: true` meaning *a response was observed*,
   not that the tool succeeded, since the response body may itself be the tool's error;
   `result` = `response`; at that span's start time). Both sources commonly exist for one
   call, and the response part recurs in every later span's history; those are not extra
   results, they are the same result observed again, and only the selected one is emitted. A
   call with neither source is a fault; more than one `execute_tool` span for one call id is
   a fault.
4. **Ordering.** Events are ordered by a total key: the timestamp in *nanoseconds* (compared
   before it is rendered to the microsecond `Timestamp`, so two events a nanosecond apart
   keep their order); then the producing span's start time and file position; then the
   *source position* within the span: system-instruction parts first (index `-1`, part
   index), then `(message index, part index)` within `gen_ai.input.messages`, then
   `(message index, part index)` within `gen_ai.output.messages` (which follow, being at the
   span's end time); then, for the same part, the mapping step. So a `tool_call_response`
   part and the user turn that follows it in the same input keep the document's order, and
   text and `tool_call` parts of one output keep theirs. Every produced event has a distinct
   (span, attribute, message index, part index, step) tuple, so ties are impossible and
   `seq` (dense from 1) is a function of the document. v1 also requires a `tool_result`
   after its `tool_call`; the completeness check enforces it.
5. **Top level.** `trace_id` = the OTLP `traceId`, which must be 32 lowercase hex characters
   (a fault otherwise: base64 ids from non-compliant producers are refused, not converted);
   `agent.name` = `gen_ai.agent.name` from the *root-most* `invoke_agent` span (the one
   without a parent, else the earliest), else `service.name`, else `unknown`; a present value
   that is not an identifier is a fault; several `invoke_agent` spans with differing names are
   recorded in `metadata` as `otel.agents: <count>` and the root-most wins, so the result is
   still a function of the document. `started_at`/`ended_at` =
   the earliest span start and the latest span end; `chart` and `currencies` absent (the
   adapter knows no books); `metadata` as above plus `otel.spans: <count>`.

The adapter is a pure function of the document: the same file yields the same trace, byte for
byte through `dump_trace` (the total ordering key above is what makes this true), and a test
asserts it on every cassette.

## Completeness validation

The adapter checks, before producing a trace, and reports every failure rather than the
first:

| Check | Why it is required |
| :-- | :-- |
| every span's `parentSpanId`, when non-empty, names a span in the document | a missing parent is a signature of sampling or a dropped batch. ADR-0002 §6 requires unsampled capture, which the adapter can only *partially* observe: it detects orphaned spans and unresolved calls, not a truncated tail (a dropped last inference span or a lost final batch leaves every parent present); the precondition remains the operator's |
| all spans share one `traceId`, and it is 32 lowercase hex characters | one trace is one run; two runs in one file are two traces; v1 needs an identifier |
| every inference span whose status is not error carries `gen_ai.output.messages` (attribute or event) | without content capture the adapter cannot know what the agent said or called; an inference span with no output is not "no output", it is "not captured" |
| every `tool_call` part has an `id`, a `name` and arguments that are an object; `id` and `name` satisfy the identifier grammar (1 to 256 characters, one line, no edge whitespace); call ids are unique across the trace | v1 requires each; the adapter does not invent or repair a call id |
| every `tool_call` has a selected result, and it is after the call | v1's pairing rule |
| every `execute_tool` span and every `tool_call_response` id matches a `tool_call` | a result without a call is a hole in the record |
| each inference span's presented conversation extends the emitted one (prefix rule) | an edited or reordered history is not the conversation that happened |
| timestamps are present, non-zero, end ≥ start, and every `intValue` is in the I-JSON safe range | a span with no time cannot be ordered; a value the trace cannot carry cannot be recorded |
| every produced field fits the v1 model: message content ≤ 65,536 characters, `arguments`/`result` ≤ 10,000 nodes and depth 32, `error.type` non-empty and ≤ 256, `error.message` ≤ 1,024, `agent.name`/`tool`/`call_id` identifiers, at most 100,000 events | checked per event *before* the document is built, so a report names the span and part, not a path into a document that was never produced; the final `load_trace` is then a self-check that must pass, and a failure there is a bug |

A report lists each failing check with the span ids, attribute keys, message and part
indices concerned (locations only, never content: the report is what an operator files, and
message text is the most sensitive thing in the document). The CLI exit code is `1` for a
report, `0` for a trace, `2` for a file that cannot be read or decoded at all (not JSON,
not I-JSON, over the byte bound).

## Redaction

An OTel export is the operator's own data: it holds message text and tool arguments as the
agent produced them, and nothing here tokenizes it. The produced v1 trace is bounded by the
v1 model (message content 65,536 characters, payloads 10,000 nodes) and refuses what does not
fit rather than truncating it, since a truncated message is a different message. Operators
who need identifiers tokenized run the journal, not the adapter; the adapter documents this
rather than offering a half-redaction.

## Cassettes

`corpus/cassettes/otel/` holds pairs: a synthesized OTLP/JSON input and the v1 trace the
adapter produces from it (or the completeness report). They are the contract tests for this
document: a change to the mapping that changes any cassette's output is a change to this
contract and is reviewed as one. Cassettes are Apache-2.0 data like the rest of `corpus/`
and carry no real content: they are synthesized to exercise each mapping step and each
completeness check.

## CLI

`ledgergate record --from-otel export.json [--out trace.json]` writes the v1 trace (to stdout
by default) or prints the completeness report to stderr and exits `1`. `--from-otel` is the
only source in M5 and is required; the parser refuses `record` without it. The subcommand
name is the roadmap's; "record" is what the adapter does with an observation. The
live-capture meaning the stub carried ("record a cassette from a live agent run", the
`cli` help text) is withdrawn by M5's implementation: nothing in M5 attaches to a running agent, and
a wrapper that did would be the "thin framework wrapper" ADR-0002 names as a convenience
over this adapter, not shipped in M5. This change also updates the README (the adapters line
of the diagram marks `openai | anthropic | langgraph` as future conveniences over OTel; the
M5 roadmap row drops "thin framework wrappers") and ADR-0002 §6 (completeness is validated
against the **v1** contract, the ingest format, and the result lifts to v2).

## What this document does not claim

- **Thin framework wrappers** (openai, anthropic, langgraph): not shipped in M5; the README
  diagram marks them as future conveniences over OTel.
- **Joining observation to journal**: an observational trace and a journal-derived trace of
  the same run are two documents; correlating them by `call_id` is future work, named in
  ADR-0002 §6 by this change.
- **Live receivers**: file input only.
- **Conventions other than 1.37**: refused when a `schemaUrl` says so; otherwise assumed,
  and the assumption is recorded in `metadata`.
