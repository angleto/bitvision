"""Tests for the canonical JSON serialiser.

Two equivalent payloads must hash identically; two distinct payloads must
hash differently. These tests cover the core invariants and the type
extensions specific to this project (UUID, datetime, Decimal, bytes).
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, date, datetime, timezone
from decimal import Decimal

import pytest

from bvphoenix.services.canonical import canonicalize, payload_hash


class TestDeterminism:
    def test_dict_key_order_does_not_matter(self) -> None:
        a = {"b": 1, "a": 2, "c": 3}
        b = {"c": 3, "a": 2, "b": 1}
        assert canonicalize(a) == canonicalize(b)
        assert payload_hash(a) == payload_hash(b)

    def test_nested_dict_key_order_does_not_matter(self) -> None:
        a = {"outer": {"z": 1, "y": 2}}
        b = {"outer": {"y": 2, "z": 1}}
        assert canonicalize(a) == canonicalize(b)

    def test_distinct_payloads_distinct_hashes(self) -> None:
        assert payload_hash({"a": 1}) != payload_hash({"a": 2})
        assert payload_hash({"a": 1}) != payload_hash({"b": 1})
        assert payload_hash([1, 2]) != payload_hash([2, 1])

    def test_idempotent_under_json_roundtrip(self) -> None:
        # canonicalize(json.loads(canonicalize(x))) == canonicalize(x)
        original = {"name": "ciao", "n": 42, "tags": ["a", "b"]}
        first = canonicalize(original)
        roundtripped = canonicalize(json.loads(first.decode("utf-8")))
        assert first == roundtripped

    def test_no_whitespace_in_output(self) -> None:
        out = canonicalize({"a": [1, 2, 3], "b": {"c": True}})
        # Tight separators: no spaces anywhere.
        assert b" " not in out
        assert b"\n" not in out

    def test_output_is_utf8_no_bom(self) -> None:
        out = canonicalize({"k": "vàlue with àccents"})
        # Must be parseable as UTF-8 and not BOM-prefixed.
        assert not out.startswith(b"\xef\xbb\xbf")
        assert "vàlue with àccents" in out.decode("utf-8")


class TestTypeCoercion:
    def test_uuid_serialises_lower_case_dashed(self) -> None:
        u = uuid.UUID("12345678-1234-5678-1234-567812345678")
        out = canonicalize({"id": u})
        assert out == b'{"id":"12345678-1234-5678-1234-567812345678"}'

    def test_aware_datetime_serialises_iso8601_z(self) -> None:
        dt = datetime(2026, 4, 26, 12, 30, 45, tzinfo=UTC)
        out = canonicalize({"at": dt})
        assert out == b'{"at":"2026-04-26T12:30:45Z"}'

    def test_datetime_with_offset_normalised_to_utc(self) -> None:
        from datetime import timedelta

        rome = timezone(timedelta(hours=2))
        dt = datetime(2026, 4, 26, 14, 30, 45, tzinfo=rome)
        out = canonicalize({"at": dt})
        # 14:30 +02:00 -> 12:30 UTC
        assert out == b'{"at":"2026-04-26T12:30:45Z"}'

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="naive datetime"):
            canonicalize({"at": datetime(2026, 4, 26, 12, 30)})

    def test_date_serialises_iso(self) -> None:
        d = date(2026, 4, 26)
        out = canonicalize({"d": d})
        assert out == b'{"d":"2026-04-26"}'

    def test_decimal_serialises_as_string(self) -> None:
        # Must NOT silently coerce to float (precision loss).
        out = canonicalize({"price": Decimal("0.1")})
        assert out == b'{"price":"0.1"}'

    def test_bytes_serialise_as_base64url_no_padding(self) -> None:
        # base64url(b"hello") = "aGVsbG8=" → no padding: "aGVsbG8"
        out = canonicalize({"raw": b"hello"})
        assert out == b'{"raw":"aGVsbG8"}'

    def test_bytearray_treated_like_bytes(self) -> None:
        out_a = canonicalize({"raw": b"hi"})
        out_b = canonicalize({"raw": bytearray(b"hi")})
        assert out_a == out_b


class TestUnicodeNormalisation:
    def test_nfc_string_normalisation_in_values(self) -> None:
        # "à" can be either NFC (U+00E0) or NFD (a + U+0300). The two
        # forms are visually identical but byte-distinct; NFC must win.
        nfc = "Café"
        nfd = "Café"
        assert nfc != nfd
        assert canonicalize({"x": nfc}) == canonicalize({"x": nfd})
        assert payload_hash({"x": nfc}) == payload_hash({"x": nfd})

    def test_nfc_string_normalisation_in_keys(self) -> None:
        nfc_key = "Café"
        nfd_key = "Café"
        assert canonicalize({nfc_key: 1}) == canonicalize({nfd_key: 1})


class TestRejection:
    def test_nan_rejected(self) -> None:
        with pytest.raises(ValueError, match="NaN"):
            canonicalize({"x": math.nan})

    def test_positive_infinity_rejected(self) -> None:
        with pytest.raises(ValueError, match="NaN/Inf"):
            canonicalize({"x": math.inf})

    def test_negative_infinity_rejected(self) -> None:
        with pytest.raises(ValueError, match="NaN/Inf"):
            canonicalize({"x": -math.inf})

    def test_non_string_dict_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-string dict key"):
            canonicalize({1: "value"})  # type: ignore[dict-item]

    def test_unsupported_type_rejected(self) -> None:
        class Custom:
            pass

        with pytest.raises(TypeError, match="cannot canonicalise"):
            canonicalize({"x": Custom()})


class TestNestedStructures:
    def test_deeply_nested_payload(self) -> None:
        payload = {
            "patient": {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
                "demographics": {
                    "name": "Mario",
                    "born": date(1970, 1, 1),
                    "tags": ["diabete", "ipertensione"],
                },
            },
            "studies": [
                {"id": uuid.UUID("11111111-1111-1111-1111-111111111111"), "n": 1},
                {"id": uuid.UUID("22222222-2222-2222-2222-222222222222"), "n": 2},
            ],
        }
        out = canonicalize(payload)
        # Order-insensitive comparison via parsed equivalent
        assert json.loads(out)["patient"]["demographics"]["name"] == "Mario"
        # Reproducible: rerun yields same bytes
        assert canonicalize(payload) == out

    def test_list_order_is_preserved(self) -> None:
        # Lists are ordered: [1, 2] != [2, 1].
        assert canonicalize({"x": [1, 2]}) != canonicalize({"x": [2, 1]})

    def test_tuple_treated_as_list(self) -> None:
        assert canonicalize({"x": (1, 2)}) == canonicalize({"x": [1, 2]})


class TestHashShape:
    def test_payload_hash_returns_32_bytes(self) -> None:
        assert len(payload_hash({"any": "value"})) == 32

    def test_payload_hash_matches_sha256_of_canonical_bytes(self) -> None:
        import hashlib

        payload = {"a": 1, "b": [2, 3]}
        expected = hashlib.sha256(canonicalize(payload)).digest()
        assert payload_hash(payload) == expected
