"""The OpenTelemetry GenAI adapter against docs/spec/otel-adapter.md: cassettes as contract,
determinism, framing, normalisation, the mapping, completeness checks, exit codes."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from ledgergate.adapters.otel import (
    MAX_FILE_BYTES,
    Report,
    UnreadableError,
    convert,
    read_export,
    self_check,
)
from ledgergate.cli.__main__ import main
from ledgergate.trace.io import dump_trace, load_any

CASSETTES = Path("corpus/cassettes/otel")
T0 = 1_700_000_000_000_000_000


def _kv(key: str, value: Any) -> dict[str, Any]:
    return {"key": key, "value": value}


def _s(v: str) -> dict[str, Any]:
    return {"stringValue": v}


def _sj(v: Any) -> dict[str, Any]:
    return {"stringValue": json.dumps(v)}


def _span(
    sid: str, parent: str | None, start: int, end: int, op: str, attrs: list[Any], **extra: Any
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": sid,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(end),
        "attributes": [_kv("gen_ai.operation.name", _s(op)), *attrs],
    }
    if parent:
        d["parentSpanId"] = parent
    d.update(extra)
    return d


def _export(spans: list[dict[str, Any]], **scope: Any) -> dict[str, Any]:
    sc: dict[str, Any] = {"scope": {"name": "acme", "version": "1"}, "spans": spans}
    sc.update(scope)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_kv("service.name", _s("acme-agent"))]},
                "scopeSpans": [sc],
            }
        ]
    }


U1: dict[str, Any] = {"role": "user", "parts": [{"type": "text", "content": "hi"}]}
A1 = {
    "role": "assistant",
    "parts": [
        {"type": "tool_call", "id": "c1", "name": "balance", "arguments": {"account": "cash"}}
    ],
}
R1 = {
    "role": "tool",
    "parts": [{"type": "tool_call_response", "id": "c1", "response": {"balance": "5"}}],
}
A2: dict[str, Any] = {"role": "assistant", "parts": [{"type": "text", "content": "five"}]}


def _two_turns() -> list[dict[str, Any]]:
    return [
        _span(
            "b" * 16,
            None,
            T0,
            T0 + 10,
            "chat",
            [_kv("gen_ai.input.messages", _sj([U1])), _kv("gen_ai.output.messages", _sj([A1]))],
        ),
        _span(
            "c" * 16,
            None,
            T0 + 11,
            T0 + 12,
            "execute_tool",
            [_kv("gen_ai.tool.call.id", _s("c1")), _kv("gen_ai.tool.name", _s("balance"))],
        ),
        _span(
            "d" * 16,
            None,
            T0 + 20,
            T0 + 30,
            "chat",
            [
                _kv("gen_ai.input.messages", _sj([U1, A1, R1])),
                _kv("gen_ai.output.messages", _sj([A2])),
            ],
        ),
    ]


def _bytes(doc: Any) -> bytes:
    return json.dumps(doc).encode()


def _findings(doc: Any) -> list[str]:
    o = convert(_bytes(doc))
    assert o.report is not None, "expected a report"
    return [f.check for f in o.report.findings]


class TestCassettes:
    @pytest.mark.parametrize(
        "path",
        sorted(
            p
            for p in CASSETTES.iterdir()
            if p.suffix in (".json", ".jsonl")
            and not p.name.endswith(".expected.json")
            and not p.name.endswith(".license")
        ),
    )
    def test_each_cassette_reproduces_its_expected_output_byte_for_byte(self, path: Path) -> None:
        data = path.read_bytes()
        outcome = convert(data)
        stem = path.with_suffix("")
        expected_trace = stem.with_suffix(".expected.json")
        expected_report = stem.with_suffix(".report.txt")
        if expected_trace.exists():
            assert outcome.report is None
            assert outcome.trace is not None
            assert dump_trace(self_check(outcome.trace)) == expected_trace.read_text()
            # determinism: twice
            again = convert(data)
            assert (
                again.trace is not None
                and dump_trace(self_check(again.trace)) == expected_trace.read_text()
            )
            # lifts and verifies as no_evidence, never passes
            lifted = load_any(expected_trace.read_text())
            assert lifted.schema_version == "2" and not list(lifted.resolutions())
        else:
            assert outcome.report is not None
            assert outcome.report.render() + "\n" == expected_report.read_text()
            assert "hi" not in outcome.report.render()  # locations only

    def test_cassettes_carry_no_content_in_reports(self) -> None:
        for report in CASSETTES.glob("*.report.txt"):
            assert "bookkeeper" not in report.read_text()


class TestFraming:
    def test_array_concatenated_and_jsonl_are_the_same_documents(self) -> None:
        doc = _export(_two_turns())
        one = convert(_bytes(doc)).trace
        arr = convert(_bytes([doc])).trace
        pretty = convert(json.dumps(doc, indent=2).encode()).trace
        assert one == arr == pretty
        two_docs = _export(_two_turns()[:2]), _export(_two_turns()[2:])
        jsonl = (json.dumps(two_docs[0]) + "\n" + json.dumps(two_docs[1]) + "\n").encode()
        assert convert(jsonl).trace == one

    def test_pre_structure_refusals_are_unreadable(self) -> None:
        for bad in (
            b"",
            b"   ",
            b"[]",
            b"not json",
            b'{"a":1} trailing',
            b'{"a": 1' + b"[" * 100_000,
        ):
            with pytest.raises(UnreadableError):
                read_export(bad)
        with pytest.raises(UnreadableError, match="bytes"):
            read_export(b"x" * (MAX_FILE_BYTES + 1))
        with pytest.raises(UnreadableError) as info:
            read_export(b'{"startTimeUnixNano": 1700000000000000000}')
        assert info.value.hint is not None and "timestamps" in info.value.hint

    def test_a_later_array_document_in_concatenated_framing_is_a_located_fault(self) -> None:
        data = (json.dumps(_export(_two_turns())) + "\n[1]\n").encode()
        report = convert(data).report
        assert report is not None and "shape" in [f.check for f in report.findings]


class TestMapping:
    def test_happy_path_events_and_metadata(self) -> None:
        o = convert(_bytes(_export(_two_turns())))
        assert o.report is None and o.trace is not None
        kinds = [e["type"] for e in o.trace["events"]]
        assert kinds == ["message", "tool_call", "tool_result", "message"]
        call, result = o.trace["events"][1], o.trace["events"][2]
        assert (
            call["call_id"] == "c1"
            and call["tool"] == "balance"
            and call["arguments"] == {"account": "cash"}
        )
        assert (
            result["ok"] is True and "result" not in result
        )  # execute_tool preferred; no captured result
        assert o.trace["agent"] == {"name": "acme-agent"}
        assert (
            o.trace["metadata"]["otel.spans"] == "3"
            and o.trace["metadata"]["otel.scope_schema_url"] == "absent"
        )
        assert [e["seq"] for e in o.trace["events"]] == [1, 2, 3, 4]

    def test_response_part_is_the_result_when_no_tool_span(self) -> None:
        spans = _two_turns()
        del spans[1]
        o = convert(_bytes(_export(spans)))
        assert o.trace is not None
        result = o.trace["events"][2]
        assert result["ok"] is True and result["result"] == {"balance": "5"}

    def test_failed_tool_span_and_fallback_error_type(self) -> None:
        spans = _two_turns()
        spans[1]["status"] = {"code": 2, "message": "boom"}
        o = convert(_bytes(_export(spans)))
        assert o.trace is not None
        result = o.trace["events"][2]
        assert result["ok"] is False and result["error"] == {
            "type": "otel.status_error",
            "message": "boom",
        }
        spans[1]["attributes"].append(_kv("error.type", _s("Timeout")))
        o = convert(_bytes(_export(spans)))
        assert o.trace is not None and o.trace["events"][2]["error"]["type"] == "Timeout"

    def test_idempotency_key_is_lifted_when_an_identifier_and_left_in_arguments(self) -> None:
        spans = _two_turns()
        a1: dict[str, Any] = copy.deepcopy(A1)
        a1["parts"][0]["arguments"] = {"account": "cash", "idempotency_key": "k1"}
        spans[0]["attributes"][2] = _kv("gen_ai.output.messages", _sj([a1]))
        spans[2]["attributes"][1] = _kv("gen_ai.input.messages", _sj([U1, a1, R1]))
        o = convert(_bytes(_export(spans)))
        assert o.trace is not None
        call = o.trace["events"][1]
        assert call["idempotency_key"] == "k1" and call["arguments"]["idempotency_key"] == "k1"

    def test_repeated_turns_are_two_messages_and_a_diverged_history_is_a_fault(self) -> None:
        spans = _two_turns()
        del spans[1]
        yes = {"role": "user", "parts": [{"type": "text", "content": "yes"}]}
        spans[0]["attributes"][1] = _kv("gen_ai.input.messages", _sj([yes]))
        spans[0]["attributes"][2] = _kv("gen_ai.output.messages", _sj([A2]))
        spans[1]["attributes"][1] = _kv("gen_ai.input.messages", _sj([yes, A2, yes]))
        o = convert(_bytes(_export(spans)))
        assert o.trace is not None
        assert [e["content"] for e in o.trace["events"] if e["type"] == "message"] == [
            "yes",
            "five",
            "yes",
            "five",
        ]
        spans[1]["attributes"][1] = _kv(
            "gen_ai.input.messages",
            _sj([{"role": "user", "parts": [{"type": "text", "content": "edited"}]}, A2]),
        )
        assert "prefix" in _findings(_export(spans))

    def test_invoke_agent_is_structural_and_names_the_agent(self) -> None:
        spans = _two_turns()
        agent = _span(
            "a" * 16,
            None,
            T0 - 1,
            T0 + 100,
            "invoke_agent",
            [_kv("gen_ai.agent.name", _s("bookkeeper")), _kv("gen_ai.output.messages", _sj([A1]))],
        )
        o = convert(_bytes(_export([agent, *spans])))
        assert o.trace is not None and o.trace["agent"]["name"] == "bookkeeper"
        assert [e["type"] for e in o.trace["events"]].count("tool_call") == 1  # not emitted twice
        assert o.trace["metadata"]["otel.agents"] == "1"

    def test_event_form_content_and_one_copy_rule(self) -> None:
        spans = _two_turns()
        spans[0]["attributes"] = [_kv("gen_ai.operation.name", _s("chat"))]
        spans[0]["events"] = [
            {
                "name": "gen_ai.client.inference.operation.details",
                "attributes": [
                    _kv("gen_ai.input.messages", _sj([U1])),
                    _kv("gen_ai.output.messages", _sj([A1])),
                ],
            }
        ]
        assert convert(_bytes(_export(spans))).trace is not None
        spans[0]["attributes"].append(_kv("gen_ai.input.messages", _sj([U1])))
        assert "one_copy" in _findings(_export(spans))


class TestCompleteness:
    def test_each_named_check_fires_alone(self) -> None:
        base = _two_turns()

        def mutate(fn: Any) -> list[str]:
            spans = copy.deepcopy(base)
            fn(spans)
            return _findings(_export(spans))

        assert "parent" in mutate(lambda s: s[1].__setitem__("parentSpanId", "e" * 16))
        assert "trace_id" in mutate(lambda s: s[1].__setitem__("traceId", "f" * 32))
        assert "output_messages" in mutate(lambda s: s[0]["attributes"].pop(2))
        assert "input_messages" in mutate(lambda s: s[2]["attributes"].pop(1))
        assert "orphan" in mutate(
            lambda s: s[1]["attributes"].__setitem__(1, _kv("gen_ai.tool.call.id", _s("zz")))
        )
        assert "tool_name" in mutate(
            lambda s: s[1]["attributes"].__setitem__(2, _kv("gen_ai.tool.name", _s("other")))
        )
        assert "timestamp" in mutate(lambda s: s[1].__setitem__("endTimeUnixNano", str(T0)))
        assert "timestamp" in mutate(lambda s: s[1].__setitem__("startTimeUnixNano", "0"))
        assert "span_id" in mutate(lambda s: s[1].__setitem__("spanId", "not-hex"))
        assert "span_id" in mutate(lambda s: s[1].__setitem__("spanId", s[0]["spanId"]))
        assert "tool_span" in mutate(lambda s: s[1]["attributes"].pop(1))
        assert "convention" in _findings(
            _export(base, schemaUrl="https://opentelemetry.io/schemas/1.28.0")
        )
        assert "shape" in mutate(lambda s: s[1].__setitem__("status", {"code": 7}))
        assert "shape" in mutate(
            lambda s: s[1]["attributes"].append(_kv("error.type", {"intValue": 5}))
        )
        assert "range" in mutate(
            lambda s: s[1]["attributes"].__setitem__(
                1, _kv("gen_ai.tool.call.id", {"intValue": "9007199254740993"})
            )
        )
        assert "type" in mutate(
            lambda s: s[1]["attributes"].__setitem__(
                1, _kv("gen_ai.tool.call.id", {"intValue": "7"})
            )
        )

    def test_response_before_call_and_time_row(self) -> None:
        spans = _two_turns()
        del spans[1]
        # presenter starts before the emitter: response before call
        spans[1]["startTimeUnixNano"] = str(T0 - 5)
        found = _findings(_export(spans))
        assert "response_before_call" in found
        # presenter starts before the emitter's text output existed: time row
        spans = _two_turns()
        said = {
            "role": "assistant",
            "parts": [{"type": "text", "content": "checking"}, *A1["parts"]],
        }
        spans[0]["attributes"][2] = _kv("gen_ai.output.messages", _sj([said]))
        spans[2]["attributes"][1] = _kv("gen_ai.input.messages", _sj([U1, said, R1]))
        assert convert(_bytes(_export(spans))).report is None
        spans[2]["startTimeUnixNano"] = str(T0 + 5)  # inside chat1, which ends at T0+10
        assert "time" in _findings(_export(spans))

    def test_zero_events_and_unmapped_spans(self) -> None:
        http = _span("e" * 16, None, T0, T0 + 1, "http", [])
        http["attributes"] = []
        assert "events" in _findings(_export([http]))
        err = _two_turns()[0]
        err["status"] = {"code": 2}
        assert "events" in _findings(_export([err]))

    def test_case_insensitive_ids_are_canonicalised(self) -> None:
        spans = _two_turns()
        for s in spans:
            s["traceId"] = s["traceId"].upper()
        spans[1]["parentSpanId"] = spans[0]["spanId"].upper()
        o = convert(_bytes(_export(spans)))
        assert o.trace is not None and o.trace["trace_id"] == "0af7651916cd43dd8448eb211c80319c"

    def test_reports_never_carry_message_text(self) -> None:
        spans = _two_turns()
        secret = {"role": "user", "parts": [{"type": "text", "content": "SECRET-TEXT"}]}
        spans[2]["attributes"][1] = _kv("gen_ai.input.messages", _sj([secret]))
        o = convert(_bytes(_export(spans)))
        assert o.report is not None and "SECRET" not in o.report.render()


class TestCli:
    def test_exit_codes_and_atomic_out(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        good = tmp_path / "good.json"
        good.write_text(json.dumps(_export(_two_turns())))
        out = tmp_path / "trace.json"
        assert main(["record", "--from-otel", str(good), "--out", str(out)]) == 0
        assert load_any(out.read_text()).trace_id == "0af7651916cd43dd8448eb211c80319c"
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith("trace.json.")] == []
        assert main(["record", "--from-otel", str(good)]) == 0
        assert json.loads(capsys.readouterr().out)["schema_version"] == "1"
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(_export(_two_turns()[:1])))
        assert main(["record", "--from-otel", str(bad)]) == 1
        assert "incomplete" in capsys.readouterr().err
        unreadable = tmp_path / "u.json"
        unreadable.write_bytes(b"{")
        assert main(["record", "--from-otel", str(unreadable)]) == 2
        assert main(["record", "--from-otel", str(tmp_path / "missing.json")]) == 2

    def test_self_check_failure_is_70_without_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ledgergate.adapters import otel

        good = tmp_path / "good.json"
        good.write_text(json.dumps(_export(_two_turns())))
        real = otel.convert

        def broken(data: bytes) -> Any:
            o = real(data)
            assert o.trace is not None
            o.trace["events"][0]["content"] = "SECRET-" + "x" * 70_000  # violates the model, a bug
            return o

        monkeypatch.setattr(otel, "convert", broken)
        assert main(["record", "--from-otel", str(good)]) == 70
        err = capsys.readouterr().err
        assert "self-check failed" in err and "SECRET" not in err


def test_report_render_is_locations_only() -> None:
    from ledgergate.adapters.otel import Finding

    r = Report(
        [Finding("prefix", "[0].resourceSpans[0].scopeSpans[0].spans[2]", "diverges at position 1")]
    )
    assert r.render().splitlines()[1].startswith("  prefix at [0]")


class TestImplementationReviewFindings:
    def test_metadata_strings_are_bounded_and_grammar_checked_not_self_check_failures(self) -> None:
        doc = _export(_two_turns())
        doc["resourceSpans"][0]["scopeSpans"][0]["scope"]["name"] = "acme\nevil"
        assert "metadata" in _findings(doc)
        doc = _export(_two_turns())
        doc["resourceSpans"][0]["scopeSpans"][0]["scope"]["name"] = "n" * 1100
        assert "metadata" in _findings(doc)
        doc = _export(_two_turns())
        doc["resourceSpans"][0]["scopeSpans"][0]["scope"]["name"] = 5
        assert "shape" in _findings(doc)
        doc = _export(_two_turns())
        second = copy.deepcopy(doc["resourceSpans"][0])
        second["scopeSpans"][0]["spans"] = []
        doc["resourceSpans"][0]["schemaUrl"] = "https://x/" + "a" * 600
        second["schemaUrl"] = "https://y/" + "b" * 600
        doc["resourceSpans"].append(second)
        found = _findings(doc)
        assert "metadata" in found  # the joined value, named by key, not exit 70

    def test_read_set_is_per_span_class(self) -> None:
        spans = _two_turns()
        agent = _span(
            "a" * 16,
            None,
            T0 - 1,
            T0 + 100,
            "invoke_agent",
            [
                _kv("gen_ai.agent.name", _s("bk")),
                _kv("gen_ai.output.messages", {"bytesValue": "AA=="}),
            ],
        )
        assert (
            convert(_bytes(_export([agent, *spans]))).report is None
        )  # content on invoke_agent is unread
        spans = _two_turns()
        spans[0]["attributes"].append(_kv("gen_ai.tool.call.id", _s("x")))
        spans[0]["attributes"].append(_kv("gen_ai.tool.call.id", _s("y")))
        assert convert(_bytes(_export(spans))).report is None  # not in a chat span's read set
        spans = _two_turns()
        spans[0]["status"] = {"code": 2}
        spans[0]["attributes"][2] = _kv("gen_ai.output.messages", {"bytesValue": "AA=="})
        found = _findings(_export(spans))
        assert "shape" not in found  # an error span's output is unread

    def test_agent_name_of_the_wrong_type_is_a_fault(self) -> None:
        agent = _span(
            "a" * 16,
            None,
            T0 - 1,
            T0 + 100,
            "invoke_agent",
            [_kv("gen_ai.agent.name", {"intValue": "7"})],
        )
        assert "type" in _findings(_export([agent, *_two_turns()]))

    def test_reports_do_not_echo_call_ids(self) -> None:
        spans = _two_turns()
        del spans[1]
        del spans[1]  # no result at all
        a1: dict[str, Any] = copy.deepcopy(A1)
        a1["parts"][0]["id"] = "pay 4111-1111-1111-1111 to bob"
        spans[0]["attributes"][2] = _kv("gen_ai.output.messages", _sj([a1]))
        o = convert(_bytes(_export(spans)))
        assert o.report is not None and "4111" not in o.report.render()
        assert any(f.check == "result" for f in o.report.findings)

    def test_empty_error_type_is_a_bound_fault(self) -> None:
        spans = _two_turns()
        spans[1]["status"] = {"code": 2}
        spans[1]["attributes"].append(_kv("error.type", _s("")))
        assert "bound" in _findings(_export(spans))

    def test_root_most_agent_is_earliest_start_then_file_position(self) -> None:
        a = _span(
            "a" * 16,
            None,
            T0 - 1,
            T0 + 100,
            "invoke_agent",
            [_kv("gen_ai.agent.name", _s("first-in-file"))],
        )
        b = _span(
            "e" * 16,
            None,
            T0 - 1,
            T0 + 1,
            "invoke_agent",
            [_kv("gen_ai.agent.name", _s("shorter"))],
        )
        o = convert(_bytes(_export([a, b, *_two_turns()])))
        assert o.trace is not None and o.trace["agent"]["name"] == "first-in-file"

    def test_out_into_a_missing_directory_is_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        good = tmp_path / "good.json"
        good.write_text(json.dumps(_export(_two_turns())))
        assert (
            main(["record", "--from-otel", str(good), "--out", str(tmp_path / "nope" / "t.json")])
            == 2
        )
        assert "cannot write" in capsys.readouterr().err


class TestSecondImplementationReview:
    @pytest.mark.parametrize("sep", ["\u2028", "\x85", "\x9b", "\x1c"])
    def test_unicode_line_separators_and_c1_controls_are_faults_in_metadata(self, sep: str) -> None:
        doc = _export(_two_turns())
        doc["resourceSpans"][0]["scopeSpans"][0]["scope"]["name"] = f"acme{sep}evil"
        assert "metadata" in _findings(doc)

    def test_non_genai_scopes_are_not_held_to_the_metadata_grammar(self) -> None:
        doc = _export(_two_turns())
        plain = _span("e" * 16, None, T0, T0 + 1, "http", [])
        plain["attributes"] = []
        http: dict[str, Any] = {"scope": {"name": "http\x01", "version": "1"}, "spans": [plain]}
        doc["resourceSpans"][0]["scopeSpans"].append(http)
        assert convert(_bytes(doc)).report is None

    def test_a_result_after_its_presentation_is_a_fault(self) -> None:
        spans = _two_turns()
        spans[1]["endTimeUnixNano"] = str(
            T0 + 40
        )  # the tool span outlives the span that read its response
        assert "result_after_presentation" in _findings(_export(spans))
        spans[1]["endTimeUnixNano"] = str(T0 + 20)  # exactly at the presenter's start: allowed
        assert convert(_bytes(_export(spans))).report is None

    def test_error_type_and_tool_name_are_typed_unconditionally(self) -> None:
        spans = _two_turns()
        spans[1]["attributes"].append(_kv("error.type", {"intValue": "5"}))
        assert "type" in _findings(_export(spans))  # status ok, still a type fault
        spans = _two_turns()
        spans[1]["attributes"][2] = _kv("gen_ai.tool.name", {"intValue": "5"})
        assert "type" in _findings(_export(spans))


class TestThirdImplementationReview:
    def test_a_bound_fault_does_not_blame_a_later_span(self) -> None:
        spans = _two_turns()
        big = {
            "role": "assistant",
            "parts": [
                {"type": "text", "content": "x" * 70_000},
                {"type": "text", "content": "ok"},
                *A1["parts"],
            ],
        }
        spans[0]["attributes"][2] = _kv("gen_ai.output.messages", _sj([big]))
        spans[2]["attributes"][1] = _kv("gen_ai.input.messages", _sj([U1, big, R1]))
        found = _findings(_export(spans))
        assert found.count("bound") == 1 and "prefix" not in found

    def test_agent_fault_is_located_at_the_agent_span(self) -> None:
        agent = _span(
            "a" * 16,
            None,
            T0 - 1,
            T0 + 100,
            "invoke_agent",
            [_kv("gen_ai.agent.name", _s(" bad "))],
        )
        o = convert(_bytes(_export([agent, *_two_turns()])))
        assert o.report is not None
        agent_faults = [f for f in o.report.findings if f.check == "agent"]
        assert agent_faults and agent_faults[0].location.endswith("spans[0]")

    def test_a_stdout_write_failure_is_exit_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import io
        import sys

        good = tmp_path / "good.json"
        good.write_text(json.dumps(_export(_two_turns())))

        class Broken(io.StringIO):
            def write(self, _s: str) -> int:
                raise BrokenPipeError

        monkeypatch.setattr(sys, "stdout", Broken())
        assert main(["record", "--from-otel", str(good)]) == 2
        assert "cannot write" in capsys.readouterr().err


class TestFourthImplementationReview:
    def test_closed_pipe_on_stdout_exits_2_with_no_interpreter_noise(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        good = tmp_path / "good.json"
        good.write_text(json.dumps(_export(_two_turns())))
        proc = subprocess.Popen(
            [sys.executable, "-m", "ledgergate.cli", "record", "--from-otel", str(good)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        proc.stdout.close()  # the reader goes away before the writer writes
        _, err = proc.communicate(timeout=60)
        assert proc.returncode == 2, err
        assert b"Exception ignored" not in err and b"cannot write" in err

    def test_over_bound_system_instruction_is_located_at_the_instruction(self) -> None:
        spans = _two_turns()
        spans[0]["attributes"].append(
            _kv("gen_ai.system_instructions", _sj([{"type": "text", "content": "s" * 70_000}]))
        )
        o = convert(_bytes(_export(spans)))
        assert o.report is not None
        bound = [f for f in o.report.findings if f.check == "bound"]
        assert bound and bound[0].location.endswith("gen_ai.system_instructions[0]")

    def test_decode_failure_names_the_document_ordinal(self) -> None:
        data = (json.dumps(_export(_two_turns())) + "\n" + '{"resourceSpans":[}' + "\n").encode()
        with pytest.raises(UnreadableError, match="document 1"):
            read_export(data)

    def test_undecodable_input_does_not_manufacture_a_prefix_fault(self) -> None:
        spans = _two_turns()
        spans[2]["attributes"][1] = _kv("gen_ai.input.messages", _s("not json"))
        found = _findings(_export(spans))
        assert "content" in found and "prefix" not in found

    def test_response_result_over_the_payload_bound_is_located_at_the_part(self) -> None:
        spans = _two_turns()
        del spans[1]
        huge = {
            "role": "tool",
            "parts": [
                {"type": "tool_call_response", "id": "c1", "response": {"k": list(range(20_000))}}
            ],
        }
        spans[1]["attributes"][1] = _kv("gen_ai.input.messages", _sj([U1, A1, huge]))
        o = convert(_bytes(_export(spans)))
        assert o.report is not None
        bound = [f for f in o.report.findings if f.check == "bound"]
        assert bound and bound[0].location.endswith("gen_ai.input.messages[2].parts[0]")
