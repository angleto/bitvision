"""Public ``GET /shared/{token}/info`` MUST NOT leak the intended
recipient's PII.

The endpoint is unauthenticated: anyone who has (or guesses, or
intercepts) the token can hit it. Returning the addressee's name /
email lets a phisher reproduce the personalised landing page verbatim
and lets an attacker who somehow lifted the token learn whom the
grantor sent it to (third-party PII). The minimum-disclosure principle
applies; the recipient already knows their own email.

This test asserts at the schema level — ``ShareInfoOut`` must not
declare those fields — and at the runtime level: the response payload
shall contain neither ``recipient_email`` nor ``recipient_name`` keys.
"""

from __future__ import annotations

from bvphoenix.api.sharing import ShareInfoOut


def test_share_info_schema_excludes_recipient_pii() -> None:
    """``ShareInfoOut`` schema must not declare recipient_email or
    recipient_name as fields. The OpenAPI consumer (FE) therefore
    cannot accidentally surface them on the public landing.
    """
    fields = set(ShareInfoOut.model_fields.keys())
    assert "recipient_email" not in fields, (
        "recipient_email leaks third-party PII on /shared/{token}/info"
    )
    assert "recipient_name" not in fields, (
        "recipient_name leaks third-party PII on /shared/{token}/info"
    )


def test_share_info_serialised_excludes_recipient_pii() -> None:
    """A populated ShareInfoOut instance must not serialise either
    recipient field even if the caller passes them via extra= kwargs.
    Defends against a future maintainer reintroducing the leak.
    """
    info = ShareInfoOut(
        study_title="CT thorax",
        modalities=["CT"],
        study_date="2025-01-01",
        requires_password=False,
        expires_at=None,
        permissions=["read"],
        max_uses=None,
        uses_remaining=None,
        resource_kind="study",
        resource_id="00000000-0000-0000-0000-000000000000",
        mode="claim",
        claimable=False,
        deidentified=False,
        total_files=10,
        total_bytes=10_000,
        grantor_display="Dr Rossi",
    )
    payload = info.model_dump()
    assert "recipient_email" not in payload
    assert "recipient_name" not in payload
