"""RFC 8785 serialization and RFC 7493 decoding: the digests the journal depends on."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledgergate.codec import (
    MAX_SAFE_INTEGER,
    IJsonError,
    JcsError,
    canonical_text,
    digest,
    loads,
)


class TestJcs:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1e16, "10000000000000000"),
            (5.0, "5"),
            (1e21, "1e+21"),
            (1e-7, "1e-7"),
            (0.000001, "0.000001"),
            (123.456, "123.456"),
            (-0.0, "0"),
            (0.1, "0.1"),
            (1.5e300, "1.5e+300"),
            (9007199254740991.0, "9007199254740991"),
        ],
    )
    def test_es_number_formatting(self, value: float, expected: str) -> None:
        assert canonical_text(value) == expected

    def test_rfc_8785_appendix_vector(self) -> None:
        doc = {
            "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
            "string": "\u20ac$\u000f\u000aA'\u0042\u0022\u005c\\\"\u002f",
            "literals": [None, True, False],
        }
        expected = (
            '{"literals":[null,true,false],'
            '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
            '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
        )
        assert canonical_text(doc) == expected

    def test_keys_sort_by_utf16_code_units_not_code_points(self) -> None:
        # U+10000 is a surrogate pair (D800 DC00) and sorts before U+FFFF in UTF-16.
        assert canonical_text({"\uffff": 2, "\U00010000": 1}) == '{"𐀀":1,"￿":2}'
        assert json.dumps({"\uffff": 2, "\U00010000": 1}, sort_keys=True, ensure_ascii=False) != (
            canonical_text({"\uffff": 2, "\U00010000": 1})
        )

    def test_integers_beyond_safe_range_are_refused(self) -> None:
        assert canonical_text(MAX_SAFE_INTEGER) == str(MAX_SAFE_INTEGER)
        with pytest.raises(JcsError, match="safe range"):
            canonical_text(MAX_SAFE_INTEGER + 1)
        with pytest.raises(JcsError):
            canonical_text({"a": 2**60})

    def test_non_json_values_are_refused(self) -> None:
        with pytest.raises(JcsError):
            canonical_text(object())
        with pytest.raises(JcsError):
            canonical_text({1: "a"})
        with pytest.raises(JcsError):
            canonical_text(float("nan"))

    def test_digest_is_stable_across_key_order_and_whitespace(self) -> None:
        a = {"b": [1, 2.5, "x"], "a": {"z": None, "y": True}}
        b = json.loads('{ "a" : {"y": true, "z": null}, "b": [1, 2.5, "x"] }')
        assert digest(a) == digest(b)
        assert len(digest(a)) == 64

    @given(
        st.recursive(
            st.none()
            | st.booleans()
            | st.integers(-MAX_SAFE_INTEGER, MAX_SAFE_INTEGER)
            | st.floats(allow_nan=False, allow_infinity=False, min_value=-1e15, max_value=1e15)
            | st.text(),
            lambda inner: st.lists(inner) | st.dictionaries(st.text(), inner),
            max_leaves=20,
        )
    )
    def test_canonical_text_is_valid_json_that_round_trips_to_an_equal_value(
        self, value: object
    ) -> None:
        text = canonical_text(value)
        back = json.loads(text)
        assert canonical_text(back) == text
        # Numeric identity survives: JCS treats 5.0 and 5 as the same number by design.
        assert _numbers_equal(back, value)


def _numbers_equal(a: object, b: object) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_numbers_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_numbers_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return float(a) == float(b)
    return a == b


class TestIJson:
    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ('{"amount": 1, "amount": 2}', "duplicate member name"),
            ("[NaN]", "not a JSON number"),
            ("[Infinity]", "not a JSON number"),
            ("[-Infinity]", "not a JSON number"),
            ("1e400", "not a finite double"),
            (str(MAX_SAFE_INTEGER + 1), "safe range"),
            (str(-(MAX_SAFE_INTEGER + 1)), "safe range"),
            ('"\\ud800"', "unpaired surrogate"),
            ('{"k\\udc00": 1}', "unpaired surrogate"),
        ],
    )
    def test_every_i_json_rule_is_enforced(self, text: str, message: str) -> None:
        with pytest.raises(IJsonError, match=message):
            loads(text)

    def test_stdlib_alone_would_have_accepted_them(self) -> None:
        """The reason this module exists."""
        assert json.loads('{"a": 1, "a": 2}') == {"a": 2}
        assert json.loads("1e400") == float("inf")
        assert json.loads('"\\ud800"') == "\ud800"

    def test_valid_i_json_round_trips(self) -> None:
        value = loads('{"x": [1, 2.5, "s", null, true, {"k": "\u20ac"}]}')
        assert value == {"x": [1, 2.5, "s", None, True, {"k": "€"}]}
        assert loads(b'{"b": "bytes ok"}') == {"b": "bytes ok"}

    def test_invalid_utf8_bytes_are_an_i_json_error(self) -> None:
        with pytest.raises(IJsonError, match="UTF-8"):
            loads(b'"\xff"')

    def test_not_json_at_all_is_the_stdlib_error(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            loads("{not json")
