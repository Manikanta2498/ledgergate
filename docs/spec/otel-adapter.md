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
alike (`require_ijson` is iterative and the scanner's recursion budget is far above 200).
Every refusal before any OTLP structure exists (not JSON: `json.JSONDecodeError`; not I-JSON
or over the byte, node or depth bound: `IJsonError`) is exit `2`, "cannot read", with the
decoder's message; the adapter never string-matches messages. The codec gains one
distinguishable subclass, `IJsonRangeError(IJsonError)`, raised for an integer outside the
safe range (whether by literal length or by value), and that *type* is the only thing the CLI
keys a hint on (below). Exit `1` is reserved for a completeness report, which presupposes a
decoded document: **after decode, any deviation from the OTLP/JSON shape this document reads**
(a top level that is not an object or an array of objects; `resourceSpans`, `scopeSpans`,
`spans`, `attributes`, `events` not arrays; a span that is not an object; an attribute
`value` with zero or several typed members; a non-integer `status.code`; a malformed
`events[]` entry) **is a completeness fault located by index path, exit `1`; nothing after
decode exits `2`.**
Fifty million nodes materialised as Python objects is several gigabytes of heap; the byte
bound does not prevent that, and an operator with such an export should split it by trace
first. Stated, so it is not rediscovered as a bug. The payload
bounds of the *produced* events are the v1 model's and are checked per event (below). Anything
else (protobuf, OTLP/HTTP framing, a live receiver) is out of scope: the adapter reads a file.

**OTLP/JSON encoding.** Attribute values arrive typed (`stringValue`, `boolValue`,
`intValue` and the nano timestamp fields as *decimal strings*, `doubleValue`, `arrayValue`,
`kvlistValue`, `bytesValue`); `status.code` is an integer (`0` unset, `1` ok, `2` error).
OTLP/JSON is proto3 JSON, which *omits* a member holding its default: an absent `status` or
`status.code` is `0`; absent `attributes`, `events`, `resourceSpans`, `scopeSpans` or `spans`
are empty arrays; an absent `parentSpanId` is the empty string (a root). A member that is
*present* with the wrong type is the shape fault above; absence is never one, so an absent
`key` is the empty string, which is outside the read set and ignored, and an absent `value`
normalises to an empty `AnyValue`, which on a read-set attribute then fails the
zero-typed-members rule (a located fault, since the read set requires a value). A read-set
attribute this document consumes as a string or identifier (`gen_ai.operation.name`,
`gen_ai.tool.call.id`, `gen_ai.tool.name`, `gen_ai.agent.name`, `service.name`, `error.type`)
that normalises to a non-string is a located fault. For an attribute outside the read set only `key` is examined (a *present*
non-string `key` is a shape fault, since the read set cannot be decided without it); its
`value` is not looked at,
though a document-level decode refusal (an `intValue` emitted as a JSON number above 2^53
anywhere in the file) is unaffected by the read set, since it happens before any structure
exists. The
nano timestamp fields must match `^[0-9]{1,19}$` (at most 1e19 - 1 ns, year 2286; the
grammar is the only bound and it renders to a `datetime` without overflow); anything else is
a completeness fault naming the span. A producer
that emits them as JSON *numbers* is refused at decode (exit `2`), since 1.7e18 exceeds the
I-JSON safe integer and the decoder cannot represent it; the adapter cannot know the offending
integer was a timestamp (no structure exists yet), so when the decoder raises
`IJsonRangeError` the CLI appends a fixed *hint*: "OTLP timestamps emitted as JSON numbers
are the common cause".
The adapter normalises an `AnyValue` to JSON before any mapping: strings, booleans and
doubles as themselves; `intValue` parsed, and a value outside the I-JSON safe range is a
fault naming the span and key; `arrayValue` to a JSON array; `kvlistValue` to a JSON object
(a repeated key is a fault, located by index path below the top-level attribute, since keys
inside `arguments` are agent content); `bytesValue` is a fault (bytes have no JSON form the trace can carry). Normalisation, and therefore every fault in this paragraph, applies to
exactly the attributes the adapter *reads*, enumerated as (span class, attribute) pairs:
inference span whose status is not error: `gen_ai.system_instructions`,
`gen_ai.input.messages`, `gen_ai.output.messages`; inference span whose status is error:
`gen_ai.input.messages` only (its `tool_call_response` parts are a result source; its text
parts are not examined, so a malformed one there faults nothing);
`execute_tool` span: `gen_ai.tool.call.id`, `gen_ai.tool.name` (checked when present: a
fault when it differs from the matched `tool_call`'s `name`, since a span saying a different
tool ran than was called is a completeness signal, and an identifier, not a body; the
convention makes it recommended, and an absent name is not checked), `gen_ai.tool.call.result`,
`error.type`; `invoke_agent` span: `gen_ai.agent.name` only (its content attributes are not
read); every span: `gen_ai.operation.name`, the timestamps, `status`; resource:
`service.name`. Anything else is never normalised and can fault nothing, since nothing of it
is recorded. An `intValue` string must match `^-?[0-9]{1,16}$` before it is parsed, a *length pre-filter*
(so `int()` is never asked about a 4,300-digit string; sixteen digits admit values above
2^53 - 1), and the parsed value is then compared with the I-JSON safe range, a fault when
outside it, because nothing downstream would catch it (`load_trace` is pydantic, not the
codec, and the payload check counts nodes, not magnitude); an `intValue` that is a JSON *number* rather than a string is a shape fault under *Input*
("any deviation from the OTLP/JSON shape this document reads"), located by span and key. A `status.code` outside 0, 1, 2 is a located
fault. The one-copy rule below applies to every attribute in the read set, not only content
keys: two `gen_ai.tool.call.id` or two `gen_ai.operation.name` attributes on one span are a
fault, since the adapter does not pick a winner. Content attributes may be a JSON *string* (the attribute form) or a native
structure (the event form); a string is parsed with the same decoder under the adapter's
bounds (50,000,000 nodes, depth 200; a per-attribute bound would refuse long histories the
payload bound per event later admits), an inner-parse refusal is a completeness fault located
by span and attribute key only, never forwarding the decoder's message (which names the
duplicate key: agent content) (exit `1`, since the document itself decoded); a
`tool_call` part's `arguments` string is parsed the same way, under the same bounds, and the two forms are
then one shape. One span carries one copy of each content key: a content key present both as
an attribute and in a `gen_ai.client.inference.operation.details` event, more than one such
event on a span, or a repeated key in a span's top-level `attributes[]` is a fault located by
span and key (the adapter does not pick a winner between two accounts of what the model saw).
Within decoded content, malformed structure (a message that is not an object, `parts` not an
array, a part without a string `type`, a `tool_call_response` part without an `id`) is a
located fault; part types this document does not map (`reasoning`, blobs, ...) produce
nothing. A `doubleValue` arriving as the proto3 JSON strings `NaN`, `Infinity` or
`-Infinity` is a fault: I-JSON has no such number.

Span fields used: `traceId`, `spanId`, `parentSpanId`, `name`, `startTimeUnixNano`,
`endTimeUnixNano`, `attributes[]` (`key`, `value` in OTLP's typed encoding: `stringValue`,
`intValue`, `boolValue`, `doubleValue`, `arrayValue`, `kvlistValue`), `events[]`
(`name`, `attributes[]`; `timeUnixNano` is not used), `status`. Attributes read are exactly
the (span class, attribute) pairs enumerated under *OTLP/JSON encoding*, including
`gen_ai.tool.call.result` on tool spans; resource attributes are read for `service.name` and
the instrumentation scope for its name and library version.

## Semantic conventions targeted

OpenTelemetry Semantic Conventions for Generative AI, **version 1.37** (status:
development). The adapter reads:

| Convention | Used for |
| :-- | :-- |
| `gen_ai.operation.name` on a span: `chat`, `generate_content`, `text_completion` | an **inference span**: its output messages become `message` events (role `assistant`) and its tool-call parts become `tool_call` events |
| `gen_ai.operation.name` = `invoke_agent` | a **structural span**: the parent of inference and tool spans. It produces no events and its content attributes, if any, are ignored (they repeat its children's), so a tool call is never emitted twice; it contributes only `gen_ai.agent.name` (preferred over `service.name` for `agent.name`) and its time bounds |
| `gen_ai.operation.name` = `execute_tool` | a tool execution span: `gen_ai.tool.call.id`, `gen_ai.tool.name`, `gen_ai.tool.call.result` (opt-in; recorded as the normalised `AnyValue` *as is*, a string staying a string, since a tool's result is a payload and parsing would make `"42"` and `42` indistinguishable; then the payload bound applies); its result is the `tool_result`. A span without `gen_ai.tool.call.id` (the attribute is opt-in) can match nothing and is a located fault |
| `gen_ai.input.messages`, `gen_ai.output.messages` (opt-in content attributes, JSON text) | message content, tool-call parts (`type: tool_call`, `id`, `name`, `arguments`) and tool-response parts (`type: tool_call_response`, `id`, `response`) |
| `gen_ai.system_instructions` | a `message` with role `system` |
| span events named `gen_ai.client.inference.operation.details` | the same content when the instrumentation emits it as an event rather than an attribute |
| `error.type`, span `status` | a failed `tool_result` (`ok: false`, `error.type` as the type) |

The mapping is versioned by that convention version. An export carries no convention
version except the optional `schemaUrl` fields, which most GenAI instrumentations leave
empty; the instrumentation scope's version is the library's release, not the convention's.
The *resource* `schemaUrl` describes the resource attributes and is set by the SDK (a Java SDK
resource routinely says `.../1.2x.0` under a 1.37 GenAI instrumentation), so it is never a
reason to refuse. So: the adapter refuses, with a report naming the URL, only when a
`scopeSpans[].schemaUrl` on a scope that contains spans with `gen_ai.operation.name` is
present and does not end in `/1.37.0`; two such scopes with differing *present* URLs are a
fault, and a scope without a URL beside one with a URL takes the present one.
Otherwise the adapter *assumes* 1.37 and records the assumption. `metadata` carries
`otel.semconv: "1.37"`, `otel.scope_schema_url` (the GenAI scope's URL, or `absent`),
`otel.resource_schema_urls` (the distinct resource URLs, sorted and joined with `;`, or
`absent`) and `otel.scope` (the GenAI scopes' names and library versions, sorted and joined
with `;`). Every `metadata` value is a string of at most 1,024 characters (v1's `StringMap`);
`otel.spans` and `otel.agents` (the number of distinct `gen_ai.agent.name` values across
`invoke_agent` spans, `0` when there are none) are always present, as decimal strings; a value
that would exceed the bound is a fault naming the key. A later convention is a new mapping, reviewed against this document; the adapter
does not guess at attributes it was not written for. Spans without `gen_ai.operation.name`,
or with one this document does not map (`embeddings`, `create_agent`, ...), produce no events;
they are counted in `otel.spans` and take part in the parent check.

## The mapping

Every produced event carries `at` from a span's start or end timestamp (nanoseconds since
epoch, rendered as a UTC `Timestamp`; span-event `timeUnixNano` values are not used, since the
event form of a content attribute describes the span it is attached to) and `seq` from the
ordering below.

1. **Messages.** Inference spans are processed in order of `startTimeUnixNano`, ties by
   position in the file. The adapter keeps the *emitted conversation*: a sequence of
   `(role, text)` items. For each span it builds the span's *presented conversation*: each
   text part of `gen_ai.system_instructions` as `(system, text)`, then, for each message in
   `gen_ai.input.messages` (a message has a `role`; its `parts` have a `type`), each `text`
   part as `(role, content)`, `content` being the part's string member (a missing or
   non-string `content` is a located fault). The emitted conversation must be a positional prefix of the
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
   presented history may legitimately be the one the next successful span re-presents. An
   output message's `role` is not examined: the convention fixes it to `assistant`, the
   adapter emits `assistant`, and a differing value would be a fact about the instrumentation,
   not the conversation. Its
   `tool_call_response` parts are still a *result source* and still subject to the orphan
   check (a response the agent was shown was observed, whatever the inference then did), so a
   call whose only observed response was presented to a failed final inference has a result.
   A consequence of skipping: an `execute_tool` span for a call the failed inference
   requested has no `tool_call` to match and is reported as a result without a call. A
   related consequence of reading calls only from *output* messages: a first inference span
   whose history already contains `tool_call_response` parts from an earlier session is
   reported with one orphan per such part, since the calls they answer were never observed.
   Under the prefix rule the *time* check above also holds: a span that starts before the end
   of the span whose output it re-presents (overlapping inference spans, an instrumentation
   that opens the next request early) is reported, not silently misordered. And a consequence
   of ordering in nanoseconds: a streaming instrumentation that starts and
   *ends* an `execute_tool` span before the inference span that requested it has ended places
   the result before the call, and the pairing check reports it; the check is right, and the
   instrumentation's timestamps are what they are. A second known consequence: the emitted conversation is one sequence, so a fan-out or
   multi-agent run (two `invoke_agent` subtrees with their own histories) presents two
   diverging histories and is reported as a prefix fault; per-subtree conversations are not
   designed in M5 and `otel.agents` records the count.
2. **Tool calls.** Each `tool_call` part in an inference span's `gen_ai.output.messages`
   becomes a `tool_call` event at the span's end time with `call_id` = the part's `id`,
   `tool` = the part's `name`, `arguments` = the part's `arguments` (an object, or `{}` when
   absent; a string is parsed as JSON, and a string that does not parse as an object is a
   completeness fault).
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
   `result` = `response`, and a part without a `response` member is a located fault, like a
   text part without `content`; at that span's start time). Both sources commonly exist for one
   call, and the response part recurs in every later span's history; the adapter selects by
   preference and emits one result. It does **not** compare the two bodies: frameworks
   routinely present a transformed or truncated version of a tool's output to the model, so a
   `tool_call_response` whose body differs from the `execute_tool` result is not detected,
   and the trace records what the tool span said, not what the model was shown. A
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
   text and `tool_call` parts of one output keep theirs. One more *key component* precedes
   file position: `R(e)`, which is `1` for a `tool_result` whose selected `tool_call` shares
   its nanosecond and span-start prefix (a coarse clock, a child span starting and ending
   with its parent) and `0` for every other event, so such a result sorts after every event
   at that instant, its call included, and pairing is never decided by export order, which is
   not a fact about the run; a third event at the same instant sorts by the remaining
   components as usual. The key is therefore (ns, R, span start, file position, source
   position, step), a total order on produced events, where the source position of a
   `tool_result` produced from an `execute_tool` span is the empty tuple (it is the only event
   of its span): every event has a distinct (span,
   attribute, message index, part index, step) tuple, so ties are impossible and `seq` (dense
   from 1) is a function of the document. v1 also requires a `tool_result`
   after its `tool_call`; the completeness check enforces it.
5. **Top level.** `trace_id` = the OTLP `traceId`, which must be 32 hex characters in either
   case (OTLP/JSON ids are case-insensitive hex; the adapter compares, matches and emits ids
   *lowercased*, an invertible canonicalisation of a byte string, not a repair; base64 ids
   from non-compliant producers are a fault, refused, not converted);
   `agent.name` = `gen_ai.agent.name` from the *root-most* `invoke_agent` span: the
   earliest-starting parentless one, ties by file position, and if none is parentless the
   earliest by the same key; else `service.name`; else `unknown`; a present value that is not
   an identifier is a fault. `otel.agents` records the count of distinct names, so several
   agents are visible and the result is still a function of the document. `started_at`/`ended_at` =
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
| all spans share one `traceId` (compared lowercased), and it is 32 hex characters | one trace is one run; two runs in one file are two traces; v1 needs an identifier |
| every inference span whose status is not error carries `gen_ai.output.messages` (attribute or event) | without content capture the adapter cannot know what the agent said or called; an inference span with no output is not "no output", it is "not captured" |
| every `tool_call` part has an `id` and a `name` satisfying the identifier grammar (1 to 256 characters, one line, no edge whitespace), and `arguments` absent, an object, or a string that parses to an object (the convention makes `arguments` optional; absent maps to `{}`, a mapping decision, not invented content: a parameterless tool was called with no arguments); call ids are unique across the trace | v1 requires each; the adapter does not invent or repair a call id |
| every `tool_call` has a selected result, and it is after the call in `seq` (the ordering key of step 4, decided in nanoseconds) | v1's pairing rule |
| every `execute_tool` span and every `tool_call_response` id matches a `tool_call`, and a `tool_call_response` part appears only in a span processed *after* the span that emitted its call | a result without a call is a hole in the record; a response shown to the model before the call existed is a fact out of order that the prefix rule (text parts only) cannot see |
| every `execute_tool` span's `gen_ai.tool.name`, when present, equals the matched call's `name` | the record must not say a different tool ran than was called |
| each inference span's presented conversation extends the emitted one (prefix rule) | an edited or reordered history is not the conversation that happened |
| every item of an inference span's presented prefix was emitted at an `at` no later than that span's start (equivalently, a non-error inference span does not start before the end of the span whose output it re-presents) | a message shown to the model before the trace says it was said is a fact out of order; the prefix rule sees sequence, not time, and without this row the ordered trace would silently contradict the conversation it just validated, the text analogue of the response-before-call row |
| timestamps are present, non-zero, end ≥ start, and every `intValue` the adapter reads is in the I-JSON safe range | a span with no time cannot be ordered; a value the trace cannot carry cannot be recorded |
| the document has at least one span; every `spanId` is present and 16 hex characters in either case, and values are unique when lowercased; `parentSpanId` matches by lowercased value | v1 requires `trace_id` and `started_at`, which an empty document cannot supply; a duplicated span id (a re-exported batch) would resolve parents ambiguously and emit events twice |
| `service.name`, when present on several resources, is one value | `agent.name` must be a function of the document; differing names are a fault naming the resources |
| every produced field fits the v1 model: message content ≤ 65,536 characters, `arguments`/`result` ≤ 10,000 nodes and depth 32, `error.type` non-empty and ≤ 256, `error.message` ≤ 1,024, `agent.name`/`tool`/`call_id` identifiers, at most 100,000 events | checked per event *before* the document is built, so a report names the span and part, not a path into a document that was never produced; the final `load_trace` is then a self-check that must pass, and a failure there is a bug: the CLI exits `70`, prints no report, and prints the validation errors *without input values* (pydantic's `errors(include_input=False)`; a raw traceback would echo message text), so a bug is never mistaken for a completeness finding and never leaks content |

A report lists each failing check with the span ids, attribute keys, message and part
indices concerned (locations only, never content: the report is what an operator files, and
message text is the most sensitive thing in the document; resources are named by index, and
the one *value* a report ever carries is a `schemaUrl`, which is a convention URL, not agent
content); a span whose `spanId` is missing
or malformed is named by its index path `resourceSpans[i].scopeSpans[j].spans[k]`, prefixed
`[d].` when the top level is an array of documents. The CLI exit code is `1` for a
report, `0` for a trace, `2` for a file that cannot be read or decoded at all (not JSON,
not I-JSON, over the byte, node or depth bound), and `70` for a self-check failure, which is
a bug.

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
by default) and exits `0`, or prints the completeness report to stderr and exits `1`; `2` and
`70` as above. `--from-otel` is the
only source in M5 and is required; the parser refuses `record` without it. The subcommand
name is the roadmap's; "record" is what the adapter does with an observation. The
live-capture meaning the stub carried ("record a cassette from a live agent run", the
`cli` help text) is withdrawn by M5's implementation: nothing in M5 attaches to a running agent, and
a wrapper that did would be the "thin framework wrapper" ADR-0002 names as a convenience
over this adapter, not shipped in M5. The README (the adapters line of the diagram marks
`openai | anthropic | langgraph` as future conveniences over OTel; the M5 roadmap row drops
"thin framework wrappers") and ADR-0002 §6 (completeness is validated against the **v1**
contract, the ingest format, and the result lifts to v2) say the same.

## What this document does not claim

- **Thin framework wrappers** (openai, anthropic, langgraph): not shipped in M5; the README
  diagram marks them as future conveniences over OTel.
- **Joining observation to journal**: an observational trace and a journal-derived trace of
  the same run are two documents; correlating them by `call_id` is future work, named in
  ADR-0002 §6.
- **Live receivers**: file input only.
- **Conventions other than 1.37**: refused when a GenAI scope's `schemaUrl` says so;
  otherwise assumed, and the assumption is recorded in `metadata`.
