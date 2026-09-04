
# OTel adapter cassettes

Synthesized OTLP/JSON exports and the exact output `ledgergate record --from-otel` produces
from each: `<name>.expected.json` (a v1 trace, byte for byte through `dump_trace`) or
`<name>.report.txt` (a completeness report). They are the contract tests for
`docs/spec/otel-adapter.md`: a mapping change that changes any output here is a contract
change and is reviewed as one. They carry no real content. Semantic conventions: GenAI 1.37.0
(`gen-ai-spans.md`, `gen-ai-events.md`), attribute form and event form both exercised.
