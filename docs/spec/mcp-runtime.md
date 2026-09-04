<!--
SPDX-FileCopyrightText: 2026 Venkata Sai Manikanta Yatam
SPDX-License-Identifier: Apache-2.0
-->

# The MCP runtime: `ledgergate serve` (M4)

`ledgergate serve` exposes one journal as a set of MCP tools over stdio, to one client, as
one local principal. It is a *transport*: everything it does is decode a wire message, hand
the journal one untyped JSON value, and encode what the journal committed. It holds no
state of its own that the journal does not hold, decides nothing the journal does not
decide, and returns nothing the journal did not commit. ADR-0002's roadmap row for M4 names
the four design questions this document answers before any transport code; every mechanism
named here is built in M4 and tested. M4 changes three things outside the transport, each
named where it applies: the journal gains `CapacityError` and refuses a non-identifier
principal, and `codec.loads` is made total over its refusals (below).

## Terms

- **Wire message**: one line of stdin, UTF-8, terminated by `\n` (the MCP stdio transport:
  newline-delimited JSON-RPC 2.0, no embedded newlines). A final line without a trailing
  `\n` at EOF is a message. The server writes one line per response to stdout and nothing
  else to stdout, ever; diagnostics go to stderr, and a diagnostic carries only the JSON-RPC
  error code, the byte length of the line, the *kind* of id (`integer`, `string`, `absent`,
  `invalid`, `undecoded`), the method **only if it is one of the five the server implements** (else
  `unknown`, mirroring the envelope's rule for tool names), and for a `-32000` the
  exception's *class name*, which is always a `JournalError` subclass defined in
  `ledgergate.journal` (`CapacityError`, `ConfigurationError`, `EffectError`,
  `IntegrityError`, or `JournalError` itself) and so a project identifier, never its
  message. For a `-32700` the id kind is `undecoded`. Never the id itself (it is the
  caller's `call_id`, a class-2 identifier the journal tokenizes), never a caller string,
  never message content: stderr is routinely captured to disk and sits outside the redactor.
- **Line bound**: a line longer than 16 MiB is refused before decoding (`-32700`, `id`
  `null`, unrecorded) by reading with a limit, so no line is materialised in memory beyond
  it; the remainder of that line is drained to the next `\n` in bounded chunks and
  discarded, so one over-long line yields exactly one `-32700` and cannot smuggle a second
  message in its tail; the diagnostic reports the drained total. Depth and node bounds
  apply after decoding. This joins the transport-class list in `journal.md`.
- **Request** vs **notification**: shape is judged first. A decoded value that is not a
  well-formed JSON-RPC 2.0 object (see *Wire decoding*) is answered `-32600` whether or not
  it has an `id` (JSON-RPC 2.0 §5 answers `{"foo": "boo"}` with `-32600`, `id` `null`).
  Among well-formed objects, one with an `id` member is a request and is always answered,
  once, echoing its `id`; one without an `id` member is a notification and is never
  answered (JSON-RPC 2.0 forbids it). `id: null` is not an absent `id`: it is an invalid
  request.
- **Session**: from the first byte read to EOF on stdin. One process serves one session
  over one journal. There is no second connection to serialize against: the process is the
  connection owner, and the journal's `BEGIN IMMEDIATE` serializes it against any other
  process the operator may have opened on the same file.
- **Call**: a JSON-RPC request with method `tools/call`.

## Wire decoding, first and alone

Every line is decoded with the project's I-JSON decoder (`ledgergate.codec.loads`) *before*
any other code looks at it. That decoder rejects duplicate member names, non-finite doubles,
integers outside the safe range and unpaired surrogates, and enforces the transport-class
depth and node bounds. M4 makes its refusals total: `loads` raises exactly `IJsonError` or
`json.JSONDecodeError` and nothing else escapes it. Two escapes exist today and are closed:
the `RecursionError` Python's scanner raises on deeply nested text before the depth bound
can be counted (converted into the depth refusal), and the `ValueError` `int()` raises on an
integer literal over 4,300 digits (refused by literal length first: any literal longer than
17 characters, sign included, exceeds `2**53 - 1`, so it is the safe-range refusal). A
two-kilobyte line of brackets or a five-kilobyte line of digits is a `-32700`, not a dead
session; the M4 totality test covers both. A generic JSON decoder would silently keep the last of two duplicate
members, so the client and the journal could disagree about what was sent; the project
decoder is therefore the only decoder on the input path, and no MCP library sits in front
of it. (The server does not depend on an MCP SDK: the stdio protocol is small, and a
dependency that decodes the wire before the journal sees it is exactly what this section
forbids.)

A line the decoder refuses is answered `-32700` (parse error) with `id` `null`. A decoded
value that is not a JSON-RPC 2.0 request object (`jsonrpc` not `"2.0"`, `method` not a
string, a JSON array: MCP 2025-06-18 has no batching), or whose `id` member is present but
neither a string nor an integer (`null`, `true` and `false` included: JSON booleans are not
integers, whatever the host language says), is answered `-32600` (invalid request),
echoing the `id` when a string or integer one was decoded and `null` otherwise, so a client
can correlate whenever correlation is possible. **Nothing is recorded.** This is the transport-class unrecorded failure
`journal.md` already names; the line never became a value the journal could digest. A
request whose method the server does not implement is answered `-32601` (method not found),
unrecorded, since it is not a call. A `tools/call` whose `params` is absent or not an
object is answered `-32602` (invalid params), unrecorded: the server lifts members only
from an object, and in MCP's own grammar a `tools/call` without a params object is not a
call. (An *empty* object is a call and is recorded as `invalid`, `missing_field`; the line
is drawn at the object, not at the presence of a name.)

A `tools/call` sent as a *notification* (no `id`) is refused unrecorded with one stderr
diagnostic: JSON-RPC forbids answering it, and a call whose result cannot be delivered must
not run (a write that cannot be answered would move money the client never learns of). This
is one of the transport-class refusals, unrecorded because the transport, not the journal,
is what the client violated.

## Methods

| Method | Behaviour |
| :-- | :-- |
| `initialize` | Always succeeds, returning the one protocol version the server speaks (`2025-06-18`) whatever the client requested (MCP's negotiation: the server answers with a version it supports, and a client that cannot speak it disconnects), server info (`ledgergate`, the package version), and `capabilities.tools` (no `listChanged`: the tool set is the journal's definition and does not change during a session). A `tools/call` received before `initialize` is served like any other: the client's handshake obligation is the client's, and an attempt against the ledger is recorded whether or not the client shook hands. |
| `notifications/initialized` | Acknowledged silently (a notification has no response). Sent wrongly *with* an `id`, it is a request for a method the server does not implement as one: `-32601`, id echoed. |
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

1. `tool` is `N` as given, forwarded whatever its type; absent, it is omitted. An unknown,
   missing or non-string name is not a transport error: it becomes the journal's
   `unknown_tool`, `missing_field` or `wrong_type` and is **recorded** as `invalid`, because
   the client did attempt a call against this ledger and the attempt is a fact about the
   run. (This departs from MCP's suggested `-32602` for an unknown tool, deliberately: the
   journal's record of attempts is the product.) Members of `params` other than `name`,
   `arguments` and `_meta` are ignored.
2. `call_id` is the JSON-RPC `id`, rendered `rpc-n<id>` for an integer and `rpc-s<id>` for a
   string (`rpc-n7`, `rpc-sabc`; the prefix letter keeps `7` and `"7"` distinct). The
   rendering is forwarded as given, however long or whatever it contains: the journal's
   admission decides whether it is an identifier, records a call whose `call_id` is not one
   as `invalid` under an unrecoverable call id (`journal.md`, admission step 3;
   `trace-v2.md`, `invalid-<seq>`), and bounds the envelope. The server never judges an id.
   Retrying with the same `id` is the client's choice; the journal records each attempt as
   its own invocation and the idempotency key, not the call id, decides replay.
3. If `A` is an object, `key` is `A["idempotency_key"]` and `approval` is `A["approval"]`,
   each lifted out of `A` *as given* when the member is present: a `null` key is
   `wrong_type` on a write and `unexpected_field` on a read, a `null` approval is no
   approval (admission treats it as absent), and an absent key is `missing_field` on a
   write. What remains of `A` is `arguments`. If `A` is not
   an object it is forwarded as `arguments` as given and admission records `wrong_type`; if
   `A` is absent, `arguments` is omitted, which admission treats as the empty object (a
   valid `trial_balance`, an invalid write).
4. The value handed to `Journal.handle` is exactly
   `{"tool": N?, "call_id": "rpc-…", "arguments": <rest>?, "key": <key>?, "approval": <approval>?}`
   with absent members omitted. `_meta` is not forwarded and not recorded: MCP reserves it
   for the protocol, and nothing in it is an input to the ledger.

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
key is replayed from that row, with one exception: the write that fills the journal to
capacity cannot be replayed there (a replay is an invocation), and its committed response is
then readable only from the journal itself or its trace. No acknowledgement is recorded; the
trace's `committed_response_matches_journal` row is named for exactly this.

## Failures the server cannot record

A `JournalError` raised by `handle` (the journal unavailable past its busy timeout, an I-JSON
violation the transport bounds did not catch, the journal at capacity, an effect fault such
as a repeated id or a naive clock, a configuration fault such as a policy set raising or a
component no longer matching the definition) is answered
as a JSON-RPC error `-32000` whose `data` is the exception's class name and its message
truncated to 1,024 characters (so one response line stays bounded), and the server
continues the session. Nothing was recorded, as `journal.md` states for that class. An
`IntegrityError` (the journal's rows contradict each other, or a row was built that the
trace could not carry) is answered the same way and then the server **exits non-zero**: a
journal in that state must not keep serving.

## Configuration and effects

`ledgergate serve --journal PATH [--create --chart chart.json] [--policy config.json]
[--approval-key KEY] [--token-key-file FILE] [--principal NAME]`.

The server lives in `ledgergate.mcp`, a layer between `cli` and `journal` that imports
`codec` (for `loads`) and never `trace`, `derive`, `invariants` or `runner`. The layers
contract gains that layer, and, since a layers contract only forbids upward imports, a
separate `forbidden` contract (`source_modules = ["ledgergate.mcp"]`) is what enforces the
"never"; ADR-0002's "final shape" sentence is amended to name the layer.

- Effects are the process's: a UTC wall clock (`SystemClock`, `datetime.now(UTC)`) and a
  cryptographically random id generator (`RandomIds`, `secrets.token_hex(16)` under the
  `entry-` prefix). Both satisfy the core's `Clock`/`IdGenerator` protocols; the journal
  already rejects a naive clock and a repeated or invalid id as effect faults.
- `--principal` (default `local`) is validated as an identifier before anything is opened,
  and M4 makes the journal refuse a principal that is not one when the object is built (it
  is persisted in every invocation and context, and the trace requires an identifier).
- `--policy` is a `ThresholdPolicySet` configuration document (the same JSON the definition
  stores; `ThresholdPolicySet.from_configuration`). Without it the null set runs. Custom
  sets are a library concern, not a CLI one: the CLI can only build what it can recompute.
  On an existing journal the flags that built it must be repeated (`--policy` and
  `--token-key-file`): `open` compares them against the definition and refuses a mismatch,
  and the server does not rebuild a set from the stored configuration, because doing so
  would let a journal dictate the rules a process runs rather than the operator.
- `--approval-key` is the Ed25519 *verification* key text the definition records; it is
  meaningful only with `--create` (an existing journal's key is in its definition) and is
  refused otherwise.
- `--token-key-file` selects the tokenizing admitter with that key; the CLI requires 32 or
  more bytes (its own policy; the `Tokenizer` accepts 16), and builds it with the fixed
  domain `mcp` and key version `v1` (both persisted in the definition and compared at every
  open, so a later change of either is a new journal; key identity is carried by
  `token_check`). Without it the identity admitter
  runs and the server prints one stderr warning that identifiers and free text will reach
  disk as given.
- `--create` builds the journal from `--chart` (a JSON array in the trace schema's
  `AccountDoc` shape; the file is parsed by `cli`, which may import `trace`, and handed to
  `mcp` as a `ChartOfAccounts`, so `mcp` itself never imports `trace`); without it
  the journal must exist and `open` applies every binding check the journal specifies.
  Either way the process holds the journal open for the session and closes it at EOF.

## Segmentation

A whole-journal trace is bounded at 5,000,000 events; a busy journal reaches that. M4's
answer is **rollover, not segmentation**, and the enforcer is the journal, not the server,
because the invariant must hold on every transaction and not only at open: as the first
statement inside every `BEGIN IMMEDIATE` transaction (under the write lock, beside the
binding check, so two writers cannot both pass a stale count) the journal evaluates
`9 * count(invocations) + count(events WHERE invocation IS NULL) + cost <= 5,000,000`, with
`cost` 9 for an invocation (write tool *or* audited read: a read is an invocation and derives
up to seven events, a write up to eight) and 1 for a message, nine being the number of
ordinal slots and therefore an upper bound. A
transaction that would fail it is refused as an unrecorded `CapacityError` (a
`JournalError`; added to `journal.md`'s list of failures the journal cannot record, which is
the one `journal.md` change M4 makes beyond wording). At capacity `serve` still answers
`initialize`, `ping` and `tools/list` (no journal transaction) and answers *every*
`tools/call`, reads included, `-32000 CapacityError`. The remedy is a new journal: the
operator creates one and points the server at it. An operation left `awaiting_approval` in
a full journal cannot become terminal there (an approval presentation is an invocation and
is refused like any other); as `journal.md` already says for a fatally misconfigured
journal, a pending operation does not migrate, and the caller resubmits under a new key in
the new journal.
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
