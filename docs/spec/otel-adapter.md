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
  observational trace to a journal-derived one (by `call_id`) is future work, named in
  ADR-0002, and nothing here claims it.

## Input

One OTLP/JSON document (`ExportTraceServiceRequest`: `resourceSpans[].scopeSpans[].spans[]`)
as written by the OpenTelemetry file exporter or a collector's file exporter, or a JSON array
of such documents (one per export batch). It is decoded by the project's I-JSON decoder
(`ledgergate.codec.loads`) before anything else looks at it, under the same transport bounds
as every other input; the file is bounded at 256 MiB, read with a limit. Anything else
(protobuf, OTLP/HTTP framing, a live receiver) is out of scope: the adapter reads a file.

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
| `gen_ai.operation.name` on a span: `chat`, `generate_content`, `text_completion`, `invoke_agent` | an inference span: its output messages become `message` events (role `assistant`) and its tool-call parts become `tool_call` events |
| `gen_ai.operation.name` = `execute_tool` | a tool execution span: `gen_ai.tool.call.id`, `gen_ai.tool.name`; its result is the `tool_result` |
| `gen_ai.input.messages`, `gen_ai.output.messages` (opt-in content attributes, JSON text) | message content, tool-call parts (`type: tool_call`, `id`, `name`, `arguments`) and tool-response parts (`type: tool_call_response`, `id`, `response`) |
| `gen_ai.system_instructions` | a `message` with role `system` |
| span events named `gen_ai.client.inference.operation.details` | the same content when the instrumentation emits it as an event rather than an attribute |
| `error.type`, span `status` | a failed `tool_result` (`ok: false`, `error.type` as the type) |

The mapping is versioned by that convention version and the adapter records it in the
trace's `metadata` (`otel.semconv: "1.37"`, `otel.scope: <instrumentation scope name and
version>`). A later convention is a new mapping, reviewed against this document; the
adapter does not guess at attributes it was not written for.

## The mapping

Every produced event carries `at` from a span or span-event timestamp (nanoseconds since
epoch, rendered as a UTC `Timestamp`) and `seq` from the ordering below.

1. **Messages.** For each inference span, in span order: the parts of `gen_ai.input.messages`
   with roles `system`, `user` or `assistant` become `message` events at the span's start
   time, **only for the first inference span of the trace and for any part not already
   emitted**: successive inference spans repeat the conversation so far, and the adapter
   de-duplicates by `(role, content)` against what it has emitted, so a conversation appears
   once. `gen_ai.output.messages` text parts become `message` events (role `assistant`) at the
   span's end time.
2. **Tool calls.** Each `tool_call` part in `gen_ai.output.messages` becomes a `tool_call`
   event at the span's end time with `call_id` = the part's `id`, `tool` = the part's `name`,
   `arguments` = the part's `arguments` (an object; a string is parsed as JSON, and a string
   that does not parse as an object is a completeness fault). `idempotency_key` is taken from
   `arguments.idempotency_key` if present (the `ledgergate serve` convention), and is left in
   the arguments too: the adapter does not rewrite what the agent sent.
3. **Tool results.** For each `tool_call`, its result is the first of: an `execute_tool` span
   whose `gen_ai.tool.call.id` matches, giving `ok` from the span status (`STATUS_CODE_ERROR`
   → `ok: false`, `error.type` → `error.type`, status message → `error.message`) and `result`
   from `gen_ai.tool.call.result` if the instrumentation captured it, at the span's end time;
   or a `tool_call_response` part in a later span's `gen_ai.input.messages` whose `id`
   matches, giving `ok: true` and `result` = `response`, at that span's start time. A call
   with neither is a completeness fault.
4. **Ordering.** Events are ordered by `at`, then by the producing span's position in the
   file, then by mapping step (message, tool_call, tool_result), and given dense `seq` from 1.
   v1 requires a `tool_result` after its `tool_call` and a strictly increasing `seq`; the
   ordering guarantees the latter and the completeness check enforces the former.
5. **Top level.** `trace_id` = the OTLP `traceId` (hex); `agent.name` = `service.name`, else
   `unknown`; `started_at`/`ended_at` = the earliest span start and the latest span end;
   `chart` and `currencies` absent (the adapter knows no books); `metadata` as above plus
   `otel.spans: <count>`.

The adapter is a pure function of the document: the same file yields the same trace, byte for
byte through `dump_trace`, and a test asserts it.

## Completeness validation

The adapter checks, before producing a trace, and reports every failure rather than the
first:

| Check | Why it is required |
| :-- | :-- |
| every span's `parentSpanId`, when non-empty, names a span in the document | a missing parent is the signature of sampling or a dropped batch; ADR-0002 §6 requires unsampled capture, and this is the observable consequence of it |
| all spans share one `traceId` | one trace is one run; two runs in one file are two traces |
| every inference span carries `gen_ai.output.messages` (attribute or event) | without content capture the adapter cannot know what the agent said or called; an inference span with no output is not "no output", it is "not captured" |
| every `tool_call` part has an `id`, a `name` and arguments that are an object | v1 requires each; the adapter does not invent a call id |
| every `tool_call` has exactly one result, after it | v1's pairing rule |
| every `execute_tool` span and `tool_call_response` part matches a `tool_call` | a result without a call is a hole in the record |
| timestamps are present, non-zero and end ≥ start | a span with no time cannot be ordered |
| the result, if produced, loads through `load_trace` | the v1 model is the final judge |

A report lists each failing check with the span ids and part indices concerned (ids only,
never content: the report is what an operator files, and message text is the most sensitive
thing in the document). The CLI exit code is `1` for a report, `0` for a trace, `2` for an
unreadable file.

## Redaction

An OTel export is the operator's own data: it holds message text and tool arguments as the
agent produced them, and nothing here tokenizes it. The produced v1 trace is bounded by the
v1 model (message content 65,536 characters, payloads 10,000 nodes) and refuses what does not
fit rather than truncating it, since a truncated message is a different message. Operators
who need identifiers tokenized run the journal, not the adapter; the adapter documents this
rather than offering a half-redaction.

## Cassettes

`corpus/cassettes/otel/` holds pairs: an OTLP/JSON input and the v1 trace the adapter
produces from it (or the completeness report). They are the contract tests for this
document: a change to the mapping that changes any cassette's output is a change to this
contract and is reviewed as one. Cassettes are Apache-2.0 data like the rest of `corpus/`
and carry no real content: they are synthesized to exercise each mapping step and each
completeness check.

## CLI

`ledgergate record --from-otel export.json [--out trace.json]` writes the v1 trace (to stdout
by default) or prints the completeness report to stderr and exits `1`. The subcommand name
is the roadmap's; "record" is what the adapter does with an observation. The live-capture
meaning the stub carried ("record a cassette from a live agent run") is withdrawn: nothing
in M5 attaches to a running agent, and a wrapper that did would be the "thin framework
wrapper" ADR-0002 names as a convenience over this adapter, not shipped in M5.

## What this document does not claim

- **Thin framework wrappers** (openai, anthropic, langgraph in the README diagram): not
  shipped in M5. The diagram is updated to mark them as future conveniences over OTel.
- **Joining observation to journal**: an observational trace and a journal-derived trace of
  the same run are two documents; correlating them by `call_id` is future work.
- **Live receivers**: file input only.
- **Conventions other than 1.37**: refused with a report naming the scope version found.
