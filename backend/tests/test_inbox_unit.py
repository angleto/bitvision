"""Unit tests for the patient-inbox building blocks (no DB, no S3).

Covers the capability codes, the defensive MIME parser, the
require-review policy table and the promotion component split.
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage

import pytest

from bvphoenix.services.inbox.codes import generate_code, normalize_code, split_local_part
from bvphoenix.services.inbox.mime import (
    MIN_INLINE_ATTACHMENT_BYTES,
    parse_inbound_email,
)
from bvphoenix.services.inbox.policy import should_require_review

# ---- codes -----------------------------------------------------------


def test_generate_code_length_and_alphabet() -> None:
    code = generate_code(80)
    assert len(code) == 16  # ceil(80 / 5)
    assert normalize_code(code) == code
    # Uniqueness sanity (not a randomness test, a "not constant" guard).
    assert len({generate_code(80) for _ in range(50)}) == 50


def test_generate_code_refuses_weak_entropy() -> None:
    with pytest.raises(ValueError):
        generate_code(32)


def test_normalize_code_crockford_confusions() -> None:
    # i/l → 1, o → 0, case-insensitive, grouping dashes dropped.
    assert normalize_code("ABCDEFGH") == "abcdefgh"
    assert normalize_code("a-b-c-d-e-f-g-h") == "abcdefgh"
    assert normalize_code("Il0O" + "a" * 8) == "1100" + "a" * 8


def test_normalize_code_rejects_garbage() -> None:
    assert normalize_code("short") is None
    assert normalize_code("x" * 64) is None
    assert normalize_code("has space pad") is None
    assert normalize_code("uuuuuuuu") is None  # 'u' not in Crockford


def test_split_local_part() -> None:
    code = generate_code(80)
    assert split_local_part(f"{code}+patient") == (code, "patient")
    assert split_local_part(f"{code.upper()}+PATIENT") == (code, "patient")
    assert split_local_part(code) is None  # no tag
    assert split_local_part(f"{code}+") is None  # empty tag
    assert split_local_part("not&valid+patient") is None
    # Extra pluses stay in the tag.
    assert split_local_part(f"{code}+a+b") == (code, "a+b")


# ---- MIME parsing ----------------------------------------------------


def _message_with(
    *,
    attachments: list[tuple[str, bytes, str, str]] = (),
    body: str = "Gentile paziente, in allegato il referto.",
    headers: dict[str, str] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Lab Rossi <lab@rossi.example>"
    msg["To"] = "someone@inbox.example"
    msg["Subject"] = "Referto esami"
    msg["Message-ID"] = f"<{uuid.uuid4()}@rossi.example>"
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(body)
    for filename, payload, maintype, subtype in attachments:
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return bytes(msg)


def test_parse_extracts_attachments_and_headers() -> None:
    pdf = b"%PDF-1.4 " + b"x" * 100
    raw = _message_with(
        attachments=[("referto.pdf", pdf, "application", "pdf")],
        headers={"Authentication-Results": "mx.example; spf=pass; dkim=pass; dmarc=pass"},
    )
    parsed = parse_inbound_email(raw)
    assert parsed.from_address == "lab@rossi.example"
    assert parsed.subject == "Referto esami"
    assert parsed.spf_result == "pass"
    assert parsed.dkim_result == "pass"
    assert parsed.dmarc_result == "pass"
    assert not parsed.is_auto_submitted
    assert [a.filename for a in parsed.attachments] == ["referto.pdf"]
    assert parsed.attachments[0].payload == pdf
    assert parsed.body_text and "referto" in parsed.body_text.lower()


def test_parse_drops_tiny_inline_keeps_explicit_attachment() -> None:
    tiny = b"\x89PNG tiny-logo"
    assert len(tiny) < MIN_INLINE_ATTACHMENT_BYTES
    msg = EmailMessage()
    msg["From"] = "a@b.example"
    msg["Subject"] = "s"
    msg.set_content("body")
    # Inline disposition with filename, below the floor → dropped.
    msg.add_related(
        tiny, maintype="image", subtype="png", filename="logo.png", disposition="inline"
    )
    # Explicit attachment of the same size → kept.
    msg.add_attachment(tiny, maintype="image", subtype="png", filename="kept.png")
    parsed = parse_inbound_email(bytes(msg))
    assert [a.filename for a in parsed.attachments] == ["kept.png"]


def test_parse_sanitises_traversal_filenames() -> None:
    pdf = b"%PDF-1.4 " + b"y" * 50
    raw = _message_with(attachments=[("../../etc/passwd.pdf", pdf, "application", "pdf")])
    parsed = parse_inbound_email(raw)
    assert parsed.attachments[0].filename == "passwd.pdf"


def test_parse_flags_auto_submitted() -> None:
    raw = _message_with(headers={"Auto-Submitted": "auto-replied"})
    assert parse_inbound_email(raw).is_auto_submitted
    raw2 = _message_with(headers={"Precedence": "bulk"})
    assert parse_inbound_email(raw2).is_auto_submitted


def test_parse_survives_garbage() -> None:
    parsed = parse_inbound_email(b"\x00\x01 not an email at all")
    assert parsed.attachments == []


# ---- require-review policy -------------------------------------------


def test_should_require_review_table() -> None:
    assert should_require_review("email", is_agent=False) is True
    assert should_require_review("email", is_agent=True) is True
    assert should_require_review("upload_mcp", is_agent=True) is True
    assert should_require_review("upload_mcp", is_agent=False) is True
    assert should_require_review("upload_ui", is_agent=True) is True
    assert should_require_review("upload_ui", is_agent=False) is False
    assert should_require_review("upload_ui", is_agent=False, review_requested=True) is True


# ---- promotion component split ----------------------------------------


def test_promotable_components_split() -> None:
    from bvphoenix.db.models import InboxItem
    from bvphoenix.services.inbox.promotion import promotable_components

    item = InboxItem(
        id=uuid.uuid4(),
        patient_id=uuid.uuid4(),
        inbound_email_id=uuid.uuid4(),
        source_channel="email",
        staged_prefix="_inbox/x/y",
        status="needs_review",
        etag=uuid.uuid4(),
        manifest={
            "components": [
                {"name": "ok.pdf", "s3_key": "k1"},
                {"name": "virus.zip", "s3_key": "k2"},
                {"name": "skipme.png", "s3_key": "k3"},
            ],
            "review_options": {"excluded_components": ["skipme.png"]},
        },
        auto_checks={
            "version": 1,
            "checks": {
                "clamav": {
                    "verdict": "block",
                    "details": {"components": {"virus.zip": {"status": "infected"}}},
                },
                "magic_allowlist": {
                    "verdict": "pass",
                    "details": {"components": {"ok.pdf": {"verdict": "pass"}}},
                },
            },
        },
    )
    promote, skipped = promotable_components(item)
    assert [c["name"] for c in promote] == ["ok.pdf"]
    reasons = {s["name"]: s["reason"] for s in skipped}
    assert reasons == {
        "virus.zip": "blocked_by_auto_checks",
        "skipme.png": "excluded_by_reviewer",
    }
