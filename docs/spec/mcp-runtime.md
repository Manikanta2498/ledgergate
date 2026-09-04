<!--
SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
SPDX-License-Identifier: Apache-2.0
-->

# The MCP runtime: `ledgergate serve` (M4)

`ledgergate serve` exposes one journal as a set of MCP tools over stdio, to one client, as
one local principal. It is a *transport*: everything it does is decode a wire message, hand
the journal one untyped JSON value, and encode what the journal committed. It holds no
state of its own that the journal does not hold, decides nothing the journal does not
decide, and returns nothing the journal did not commit. ADR-0002 §2 names the four design
questions this document answers before any transport code; every mechanism named here is
built in M4 and tested.

## Terms

- **Wire message**: one line of stdin, UTF-8, terminated by `\n` (the MCP stdio transport:
  newline-delimited JSON-RPC 2.0, no embedded newlines). The server writes one line per
  response to stdout and nothing else to stdout, ever; diagnostics go to stderr.
- **Session**: from the first byte read to EOF on stdin. One process serves one session
  over one journal. There is no second connection to serialize against: the process is the
  connection owner, and the journal's `BEGIN IMMEDIATE` serializes it against any other
  process the operator may have opened on the same file.
- **Call**: a JSON-RPC request with method `tools/call`.

## Wire decoding, first and alone

Every line is decoded with the project's I-JSON decoder (`ledgergate.codec.loads`) *before*
any other code looks at it. That decoder rejects duplicate member names, non-finite doubles,
integers outside the safe range and unpaired surrogates, and enforces the transport-class
depth and node bounds. A generic JSON decoder would silently keep the last of two duplicate
members, so the client and the journal could disagree about what was sent; the project
decoder is therefore the only decoder on the input path, and no MCP library sits in front
of it. (The server does not depend on an MCP SDK: the stdio protocol is small, and a
dependency that decodes the wire before the journal sees it is exactly what this section
forbids.)

A line the decoder refuses, or that is not a JSON-RPC 2.0 message, or whose `id` is not a
string or integer, is answered with a JSON-RPC error (`-32700` parse error or `-32600`
invalid request), with `id` `null` when no usable id was read. **Nothing is recorded.**
This is the transport-class unrecorded failure `journal.md` already names; the line never
became a value the journal could digest. A well-formed message the server does not
implement is answered `-32601` (method not found), also unrecorded, since it is not a call.

## Methods

| Method | Behaviour |
| :-- | :-- |
| `initialize` | Returns the protocol version the server speaks (`2025-06-18`), server info (`ledgergate`, the package version), and `capabilities.tools` (no `listChanged`: the tool set is the journal's definition and does not change during a session). The client's requested version is not negotiated down: a client that cannot speak this version gets an error and the session continues. |
| `notifications/initialized` | Acknowledged silently (a notification has no response). |
| `ping` | `{}` |
| `tools/list` | The seven tools below with their input schemas, derived from the codec's command shapes and the journal's read tools. No pagination: seven tools. |
| `tools/call` | The mapping below. |

Notifications other than `initialized` (for example `notifications/cancelled`) are
ignored: a call is one journal transaction, atomic and short; it cannot be cancelled once
begun and there is nothing to cancel before it begins, since the server handles messages
one at a time in arrival order.

## Tools

One tool per journal tool: `post`, `reverse`, `open_transaction`, `advance`, `refund`
(writes) and `balance`, `trial_balance` (reads). Each tool's `inputSchema` is a JSON Schema
for its `arguments` object **plus** two reserved top-level members the server lifts out
before the journal sees the arguments:

- `idempotency_key` (string, required on every write tool, forbidden on reads): the
  journal's `key`.
- `approval` (object, optional on write tools): an approval artefact, passed through verbatim
  for the journal to validate.

The schemas are generated from one source in code (`ledgergate.mcp.tools`), and a test
asserts that every example the codec accepts validates against the schema for its tool and
that the reserved members are named exactly `idempotency_key` and `approval` in every write
schema. The schema is advisory to the client; **admission does not trust it**: the journal
admits the same untyped value whether or not the client validated.

## The mapping from `tools/call` to a `Request`

Given `params = {"name": N, "arguments": A, "_meta": M?}` on a request with JSON-RPC `id`
`I`:

1. `tool` is `N` as given. An unknown name is not a transport error: it becomes the
   journal's `unknown_tool` and is **recorded** as `invalid`, because the client did
   attempt a call against this ledger and the attempt is a fact about the run.
2. `call_id` is the JSON-RPC `id`, rendered as `rpc-<id>` (`rpc-7`, `rpc-abc`). An `id` that
   does not render to an identifier (longer than 250 characters, or not a single line) is a
   transport error (`-32600`), unrecorded: the client has violated the transport, and no
   identifier exists to record the attempt under. Retrying with the same `id` is the
   client's choice; the journal records each attempt as its own invocation and the
   idempotency key, not the call id, decides replay.
3. `key` is `A["idempotency_key"]` if `A` is an object carrying one, lifted out of `A`;
   `approval` is `A["approval"]`, lifted out likewise. What remains of `A` is `arguments`.
   If `A` is not an object, or is absent, the value handed to the journal has
   `arguments: A` as given (or omitted), and admission records `wrong_type` or a missing
   field as `invalid`.
4. The value handed to `Journal.handle` is exactly
   `{"tool": N, "call_id": "rpc-<I>", "arguments": <rest>, "key": <key>?, "approval": <approval>?}`
   with absent members omitted, never `null`. `_meta` is not forwarded and not recorded:
   MCP reserves it for the protocol, and nothing in it is an input to the ledger.

Everything after step 4 is the journal's: admission, redaction, tokenization, disposition,
policy, approval checks, execution, the committed response. The server adds nothing.

## The response

The journal's `Response.as_tool_result()` is `{"ok": bool, "result"?: ..., "error"?: {type,
message}}`. The MCP result is:

```json
{"content": [{"type": "text", "text": "<canonical JSON of the tool result>"}],
 "structuredContent": <the tool result object>,
 "isError": <not ok>}
```

`isError` is the tool-level flag MCP defines for a call that reached the tool and failed
there; every journal disposition that is not a success (`rejected`, `denied`,
`awaiting_approval`, `conflict`, `invalid`) is exactly that, so `isError` is `not ok` and
nothing more. The JSON-RPC envelope carries no error for these: the call succeeded as a
call. `text` is the RFC 8785 rendering of the same object as `structuredContent`, so a
client reading either sees one value.

**Commit point.** The journal commits inside `handle`; the server writes the response line
after `handle` returns. The journal therefore records the *committed response payload*
(`journal.md`, `invocation_responses`); a crash between commit and the write leaves a
complete row and an undelivered response, and the client's retry with the same idempotency
key is replayed from that row. No acknowledgement is recorded; the trace's
`committed_response_matches_journal` row is named for exactly this.

## Failures the server cannot record

A `JournalError` raised by `handle` (the journal unavailable past its busy timeout, an I-JSON
violation the transport bounds did not catch, a configuration fault such as a policy set
raising or a component no longer matching the definition) is answered as a JSON-RPC error
`-32000` with the exception's class name and message as `data`, and the server continues
the session. Nothing was recorded, as `journal.md` states for that class. An
`IntegrityError` (the file contradicts itself) is answered the same way and then the
server **exits non-zero**: a journal whose rows disagree must not keep serving.

## Configuration and effects

`ledgergate serve --journal PATH [--create --chart chart.json] [--policy config.json]
[--approval-key KEY] [--token-key-file FILE] [--principal NAME]`.

- Effects are the process's: a UTC wall clock (`SystemClock`, `datetime.now(UTC)`) and a
  cryptographically random id generator (`RandomIds`, `secrets.token_hex(16)` under the
  `entry-` prefix). Both satisfy the core's `Clock`/`IdGenerator` protocols; the journal
  already rejects a naive clock and a repeated or invalid id as effect faults.
- `--policy` is a `ThresholdPolicySet` configuration document (the same JSON the definition
  stores; `ThresholdPolicySet.from_configuration`). Without it the null set runs. Custom
  sets are a library concern, not a CLI one: the CLI can only build what it can recompute.
- `--token-key-file` selects the tokenizing admitter with that key (raw 32+ bytes); without
  it the identity admitter runs and the server prints one stderr warning that identifiers
  and free text will reach disk as given.
- `--create` builds the journal from `--chart` (a JSON array of `AccountDoc`); without it
  the journal must exist and `open` applies every binding check the journal specifies.
  Either way the process holds the journal open for the session and closes it at EOF.

## Segmentation

A whole-journal trace is bounded at 5,000,000 events; a busy journal reaches that. M4's
answer is **rollover, not segmentation**: `serve` refuses to open a journal whose derived
trace would exceed the bound (an estimate from row counts: nine events per invocation plus
one per message, computed before serving), with a message naming the bound and the
remedy. The remedy is a new journal: the operator creates one and points the server at it.
A rolled-over journal is complete, verifiable and closed; the two journals share nothing
but the operator's intent, and an approval artefact is bound to one of them by `journal_id`.
Cross-journal continuity (a trace over a sequence of journals, or a chain of definitions)
is not designed here; it is named as future work in ADR-0002, and nothing in M4 claims it.

## What this document does not claim

- Authentication or multiple principals: one local principal, named by `--principal`
  (default `local`), recorded in every context. M8.
- Delivery: see the commit point above.
- Protection against a client that opens the journal file directly: the journal's own
  constraints and the operator's file permissions are the mechanism; the server adds none.
