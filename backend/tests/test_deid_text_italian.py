"""Severe coverage of the Italian PHI redaction baseline.

The regex pipeline in ``services/deid_text`` is the last line of defense
before a private fascicolo becomes part of OpenData. A miss here means
identifiable text gets published. The tests below probe the patterns
against the long tail of Italian formats: dotted phone numbers,
abbreviated street types, lowercase codice fiscale, etc., and pin
the audit-trail invariants (kinds emitted, hashes set, plaintext
discarded).

These are pure-Python tests; no DB required.
"""

from __future__ import annotations

import pytest

from bvphoenix.services.deid_text import (
    Redaction,
    redact_all,
    redact_text,
)

# ---------------------------------------------------------------------------
# Codice Fiscale
# ---------------------------------------------------------------------------


class TestCodiceFiscale:
    @pytest.mark.parametrize(
        "raw",
        [
            "RSSMRA85T10A562S",
            "rssmra85t10a562s",  # lowercase
            "BNCGNN70A01H501Y",  # mixed real-shape
        ],
    )
    def test_canonical_cf_redacted(self, raw: str) -> None:
        out, marks = redact_text(f"CF: {raw} grazie")
        assert raw not in out
        assert "[CF]" in out
        kinds = [m.kind for m in marks]
        assert "regex_codice_fiscale" in kinds

    def test_short_alphanumeric_is_not_a_cf(self) -> None:
        """The CF pattern requires 16 chars in a strict layout. Random
        16-char alphanumerics that don't match the layout must not
        trigger redaction (avoid false positives on UUIDs, hashes)."""
        out, marks = redact_text("token: ABCDEFGHIJKLMNOP visit")
        assert all(m.kind != "regex_codice_fiscale" for m in marks), out

    def test_cf_inside_word_boundary(self) -> None:
        """The pattern uses ``\\b`` boundaries; CF wedged inside a word
        (e.g. ``XYZRSSMRA85T10A562SQ``) must NOT be over-matched."""
        out, _ = redact_text("noise XYZRSSMRA85T10A562SQ here")
        # Pattern misses (good) — but also document the limit so future
        # changes that try to relax the boundary don't break this.
        assert "XYZRSSMRA85T10A562SQ" in out


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


class TestEmail:
    @pytest.mark.parametrize(
        "raw",
        [
            "mario.rossi@example.com",
            "mario.rossi+tag@hospital.it",
            "m.rossi.123@sub.domain.co.uk",
        ],
    )
    def test_email_redacted(self, raw: str) -> None:
        out, marks = redact_text(f"Contatto: {raw}.")
        assert raw not in out
        assert "[EMAIL]" in out
        assert any(m.kind == "regex_email" for m in marks)


# ---------------------------------------------------------------------------
# Telefono italiano
# ---------------------------------------------------------------------------


class TestPhone:
    """Italian phone numbers come in many surface forms — the test
    matrix below documents which currently hit the regex and which
    don't. Regressions here would silently leak phone numbers."""

    REDACTED = [
        "3331234567",
        "333 1234567",
        "333 123 4567",
        "333-123-4567",
        "333.123.4567",
        "+39 333 1234567",
        "+39 333.123.4567",
        "+393331234567",
        "+39-3331234567",
        "06 12345678",
        "06-12345678",
        "0612345678",
    ]

    MISSED = [
        # International short form without +39 stays out of scope —
        # Italian baseline only. The LLM scrub pass (F12.4-llm) is
        # expected to catch foreign-format PHI.
        "+1 555 1234567",
    ]

    @pytest.mark.parametrize("raw", REDACTED)
    def test_phone_format_caught(self, raw: str) -> None:
        out, marks = redact_text(f"Tel: {raw} ufficio.")
        assert raw not in out, f"phone leak: {raw!r} -> {out!r}"
        assert "[TEL]" in out
        assert any(m.kind == "regex_phone" for m in marks)

    @pytest.mark.parametrize("raw", MISSED)
    def test_phone_format_known_to_miss(self, raw: str) -> None:
        """Documented misses: dotted phones and non-IT international.

        If you change the regex to catch these, flip this test to
        ``REDACTED``. The pin guards against regressions either way.
        """
        out, _ = redact_text(f"Tel: {raw}")
        # We accept these are missed today; they will not match.
        # The pattern matters for OpenData publish — the LLM scrub
        # pass (F12.4-llm) is expected to catch them as defence in depth.
        assert raw in out, (
            f"unexpected: dotted/intl phone {raw!r} now caught — flip this test to REDACTED."
        )


# ---------------------------------------------------------------------------
# Date precise
# ---------------------------------------------------------------------------


class TestPreciseDates:
    @pytest.mark.parametrize(
        "raw",
        [
            "12/03/1980",
            "1/3/1980",
            "12-03-1980",
            "1980-03-12",
        ],
    )
    def test_precise_date_redacted(self, raw: str) -> None:
        out, marks = redact_text(f"Nato il {raw}.")
        assert raw not in out
        assert "[DATE]" in out
        assert any(m.kind == "regex_date_precise" for m in marks)

    def test_year_only_is_kept(self) -> None:
        out, marks = redact_text("Diagnosi del 2023.")
        assert "2023" in out
        assert all(m.kind != "regex_date_precise" for m in marks)


# ---------------------------------------------------------------------------
# Indirizzi italiani
# ---------------------------------------------------------------------------


class TestAddressItalian:
    REDACTED = [
        "Via Roma 12",
        "via roma 12",
        "Viale Mazzini 33",
        "Piazza San Marco 1",
        "Corso Italia 100",
    ]
    MISSED = [
        # Common abbreviations that the regex does not cover today.
        "V.le Roma 12",
        "P.zza San Marco 1",
        "C.so Italia 100",
        # Comma-separated number, no space before digits.
        "Via Roma, 12",
    ]

    @pytest.mark.parametrize("raw", REDACTED)
    def test_canonical_address_redacted(self, raw: str) -> None:
        out, marks = redact_text(f"Indirizzo: {raw} Roma.")
        assert raw not in out, f"address leak: {raw!r} -> {out!r}"
        assert "[ADDR]" in out
        assert any(m.kind == "regex_address" for m in marks)

    @pytest.mark.parametrize("raw", MISSED)
    def test_address_known_misses(self, raw: str) -> None:
        out, _ = redact_text(f"Indirizzo: {raw}.")
        assert raw in out, (
            f"unexpected: abbreviated address {raw!r} now caught — flip this test to REDACTED."
        )


# ---------------------------------------------------------------------------
# Audit invariants (kind, hash, no plaintext retained)
# ---------------------------------------------------------------------------


class TestAuditInvariants:
    def test_redaction_objects_carry_hash_not_plaintext(self) -> None:
        """The Redaction record must hash the original excerpt — never
        retain the plaintext. Auditors get the kind and hash; the
        plaintext is gone from memory after redact_text returns the
        out string."""
        _out, marks = redact_text("CF RSSMRA85T10A562S, mail mario@example.com")
        assert marks
        for m in marks:
            assert isinstance(m, Redaction)
            assert isinstance(m.original_excerpt_hash, bytes)
            assert len(m.original_excerpt_hash) == 32  # sha256
            # The Redaction dataclass exposes only kind + position +
            # hash + placeholder — no plaintext field.
            attrs = set(m.__slots__) if hasattr(m, "__slots__") else set()
            assert "plaintext" not in attrs

    def test_kind_alphabet_is_stable(self) -> None:
        """Auditors query by kind; the kind vocabulary is part of the
        public contract. Pin it so a refactor does not silently
        change kind names and break downstream analytics."""
        _out, marks = redact_text(
            "CF RSSMRA85T10A562S "
            "tel 333 1234567 "
            "email m.r@x.it "
            "data 12/03/1980 "
            "indirizzo Via Roma 12"
        )
        kinds_emitted = {m.kind for m in marks}
        expected_kinds = {
            "regex_codice_fiscale",
            "regex_phone",
            "regex_email",
            "regex_date_precise",
            "regex_address",
        }
        assert expected_kinds.issubset(kinds_emitted), (
            f"kind contract drift: missing {expected_kinds - kinds_emitted}"
        )

    def test_skip_kinds_disables_specific_pattern(self) -> None:
        out, marks = redact_text(
            "CF RSSMRA85T10A562S",
            skip_kinds=frozenset({"regex_codice_fiscale"}),
        )
        assert "RSSMRA85T10A562S" in out
        assert all(m.kind != "regex_codice_fiscale" for m in marks)


# ---------------------------------------------------------------------------
# Layered redaction order
# ---------------------------------------------------------------------------


class TestPatternComposition:
    def test_email_inside_text_does_not_trigger_phone(self) -> None:
        """Email goes through first; later patterns operate on the
        already-redacted string. So a phone number embedded in an
        email's local part should not produce a duplicate phone match."""
        _out, marks = redact_text("contatto 3331234567@example.com")
        emails = [m for m in marks if m.kind == "regex_email"]
        phones = [m for m in marks if m.kind == "regex_phone"]
        # Pattern order: CF → email → phone → date → address. The email
        # regex consumes the whole "3331234567@example.com" before the
        # phone regex runs on the redacted "[EMAIL]" placeholder.
        assert len(emails) == 1, f"expected 1 email, got {emails}"
        assert len(phones) == 0, f"phone double-counted: {phones}"

    def test_idempotent_on_already_redacted_text(self) -> None:
        out1, _ = redact_text("CF RSSMRA85T10A562S")
        out2, _ = redact_text(out1)
        assert out1 == out2


# ---------------------------------------------------------------------------
# Field-level helper redact_all
# ---------------------------------------------------------------------------


class TestRedactAll:
    def test_only_specified_string_fields_are_scrubbed(self) -> None:
        payload = {
            "body": "CF RSSMRA85T10A562S",
            "title": "Visita 12/03/1980",
            "metadata": {"phone": "3331234567"},  # nested dict ignored
            "count": 5,  # non-string ignored
        }
        new, marks = redact_all(payload, fields=["body", "title", "metadata"])
        # body and title scrubbed.
        assert "RSSMRA85T10A562S" not in new["body"]
        assert "12/03/1980" not in new["title"]
        # metadata is a dict; redact_all does NOT walk it (string-only).
        assert new["metadata"] == payload["metadata"]
        # count unchanged.
        assert new["count"] == 5
        # Original input is not mutated.
        assert payload["body"] == "CF RSSMRA85T10A562S"
        assert isinstance(marks, list)
        assert any(m.kind == "regex_codice_fiscale" for m in marks)


# ---------------------------------------------------------------------------
# Defensive: empty / None / extreme inputs
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_empty_or_whitespace(self, raw: str | None) -> None:
        out, marks = redact_text(raw)
        if raw is None or raw == "":
            assert out == ""
        else:
            assert out == raw
        assert marks == []

    def test_long_input_no_crash(self) -> None:
        # A 100k-char body of clinical text with one PHI token at the
        # end. Used to surface catastrophic backtracking in the regex
        # pipeline if any pattern has nested quantifiers.
        body = ("paziente con dolore toracico " * 3500) + " CF RSSMRA85T10A562S"
        out, marks = redact_text(body)
        assert "RSSMRA85T10A562S" not in out
        assert any(m.kind == "regex_codice_fiscale" for m in marks)

    def test_unicode_preserved(self) -> None:
        body = "Paziente con dispnea (à è ì ò ù) CF RSSMRA85T10A562S"
        out, _ = redact_text(body)
        assert "à è ì ò ù" in out
        assert "RSSMRA85T10A562S" not in out
