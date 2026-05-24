"""Unit tests for the ``dry_run`` branch of share-link creation.

The dry-run path is the pre-commit preview used by the MCP
``create_*_share_link`` tools: it must validate everything the real
mint validates (RBAC, agent patient scope, owner check, grantee
resolution, deidentify policy) and then bail before any DB write,
audit emission, or prep-job enqueue.

These tests exercise ``_dry_run_share_link_out`` directly because
it's pure (no DB, no S3, no audit). End-to-end coverage of the
endpoint dispatch lives in the existing
``test_share_hardening.py`` family which already pins the validation
rules; here we focus on the shape contract of the synthetic
response so the MCP wrapper can branch deterministically on
``id == 'dry-run'``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bvphoenix.api.sharing import (
    ShareCreateIn,
    ShareTarget,
    _dry_run_share_link_out,
)


def _body(**overrides) -> ShareCreateIn:
    base = ShareCreateIn(
        access_level="viewer",
        target=ShareTarget(kind="link_public"),
        mode="claim",
    )
    return base.model_copy(update=overrides)


def test_dry_run_returns_placeholder_id_and_token() -> None:
    out = _dry_run_share_link_out(
        body=_body(),
        resource_kind="study",
        resource_id="study-uuid",
        permissions=["read_metadata"],
        grantor_subject_id="subj-1",
        valid_until=None,
        deidentify=True,
    )
    # Placeholders make a dry-run impossible to confuse with a real share
    assert out.id == "dry-run"
    assert out.token == "dry-run"
    assert out.url.startswith("(dry-run")


def test_dry_run_never_returns_generated_password_even_when_autogen_true() -> None:
    """The high-entropy password is mintaged only on the real commit
    branch. A dry-run that returned one would let the agent capture a
    secret without persisting the share — exactly the exfiltration
    surface ``sensitive=True`` is meant to prevent."""
    out = _dry_run_share_link_out(
        body=_body(autogen_password=True),
        resource_kind="study",
        resource_id="study-uuid",
        permissions=["read_metadata"],
        grantor_subject_id="subj-1",
        valid_until=None,
        deidentify=True,
    )
    assert out.generated_password is None
    # ``requires_password`` still reflects the would-be state so the
    # GUI / agent can show the preview correctly.
    assert out.requires_password is True


def test_dry_run_propagates_resource_identity_and_permissions() -> None:
    """The agent reads ``resource_kind`` / ``resource_id`` /
    ``permissions`` from the dry-run to confirm it's about to share the
    right thing. Drift here would silently mislead the operator."""
    out = _dry_run_share_link_out(
        body=_body(),
        resource_kind="folder",
        resource_id="folder-uuid",
        permissions=["read_metadata", "read_pixels"],
        grantor_subject_id="subj-7",
        valid_until=None,
        deidentify=False,
    )
    assert out.resource_kind == "folder"
    assert out.resource_id == "folder-uuid"
    assert out.permissions == ["read_metadata", "read_pixels"]
    assert out.grantor_subject_id == "subj-7"
    assert out.deidentify is False


def test_dry_run_serializes_expires_at_when_valid_until_given() -> None:
    valid_until = datetime.now(UTC) + timedelta(hours=24)
    out = _dry_run_share_link_out(
        body=_body(),
        resource_kind="study",
        resource_id="study-uuid",
        permissions=[],
        grantor_subject_id="subj-1",
        valid_until=valid_until,
        deidentify=True,
    )
    assert out.expires_at == valid_until.isoformat()


def test_dry_run_normalises_blank_recipient_fields_to_none() -> None:
    """Match the real ``_link_out`` projection so the agent sees the
    same shape on dry-run and on commit."""
    out = _dry_run_share_link_out(
        body=_body(
            recipient_name="   ",
            recipient_email="   ",
            recipient_phone="",
        ),
        resource_kind="study",
        resource_id="study-uuid",
        permissions=[],
        grantor_subject_id="subj-1",
        valid_until=None,
        deidentify=True,
    )
    assert out.recipient_name is None
    assert out.recipient_email is None
    assert out.recipient_phone is None


def test_dry_run_preserves_label_and_max_uses_and_mode() -> None:
    out = _dry_run_share_link_out(
        body=_body(
            label="for Dr Rossi",
            max_uses=3,
            mode="anonymous",
            recipient_name="Dr Rossi",
            recipient_email="rossi@example.com",
        ),
        resource_kind="study",
        resource_id="study-uuid",
        permissions=["read_metadata"],
        grantor_subject_id="subj-1",
        valid_until=None,
        deidentify=True,
    )
    assert out.label == "for Dr Rossi"
    assert out.max_uses == 3
    assert out.mode == "anonymous"


def test_dry_run_lowercases_recipient_email() -> None:
    out = _dry_run_share_link_out(
        body=_body(recipient_email="Rossi@Example.COM"),
        resource_kind="study",
        resource_id="study-uuid",
        permissions=[],
        grantor_subject_id="subj-1",
        valid_until=None,
        deidentify=True,
    )
    assert out.recipient_email == "rossi@example.com"
