"""Unit tests for the share-hardening validation surface.

Covers the *pure* validation rules added in commits ``d6ca2aa``
(/studies/{id}/share) and ``3971557`` (/patients/{id}/share):

* ``autogen_password`` and ``password`` are mutually exclusive.
* ``mode='anonymous'`` requires a non-empty ``recipient_name`` and
  at least one of ``recipient_email`` / ``recipient_phone``.

The API layer wires these through ``HTTPException`` so we exercise
``_validate_share_create`` (studies) directly. The patients endpoint
inlines the same checks; the rules are shared, the test asserts the
shape of the failure for one canonical caller.

Also covers:

* ``_autogen_password`` shape — 24 chars, restricted alphabet,
  no ambiguous characters (I/O/L/0/1).
* ``ActorContext.kind='link'`` round-trip through the dataclass.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from bvphoenix.api.sharing import (
    ShareCreateIn,
    ShareTarget,
    _autogen_password,
    _validate_share_create,
)
from bvphoenix.services.versioning import ActorContext


def _claim_payload(**overrides) -> ShareCreateIn:
    base = ShareCreateIn(
        access_level="viewer",
        target=ShareTarget(kind="link_public"),
        mode="claim",
    )
    return base.model_copy(update=overrides)


def _anon_payload(**overrides) -> ShareCreateIn:
    base = ShareCreateIn(
        access_level="viewer",
        target=ShareTarget(kind="link_public"),
        mode="anonymous",
        recipient_name="Dr Rossi",
        recipient_email="rossi@example.com",
    )
    return base.model_copy(update=overrides)


def test_validate_claim_mode_default_is_accepted() -> None:
    _validate_share_create(_claim_payload())


def test_autogen_and_password_mutually_exclusive() -> None:
    body = _claim_payload(password="hunter2", autogen_password=True)
    with pytest.raises(HTTPException) as exc:
        _validate_share_create(body)
    assert exc.value.status_code == 400
    assert "mutually exclusive" in str(exc.value.detail)


def test_anonymous_requires_recipient_name() -> None:
    body = _anon_payload(recipient_name=None)
    with pytest.raises(HTTPException) as exc:
        _validate_share_create(body)
    assert exc.value.status_code == 400
    assert "recipient_name" in str(exc.value.detail)


def test_anonymous_requires_recipient_name_non_blank() -> None:
    body = _anon_payload(recipient_name="   ")
    with pytest.raises(HTTPException) as exc:
        _validate_share_create(body)
    assert exc.value.status_code == 400


def test_anonymous_requires_email_or_phone() -> None:
    body = _anon_payload(recipient_email=None, recipient_phone=None)
    with pytest.raises(HTTPException) as exc:
        _validate_share_create(body)
    assert exc.value.status_code == 400
    assert "recipient_email" in str(exc.value.detail)


def test_anonymous_phone_only_is_accepted() -> None:
    body = _anon_payload(recipient_email=None, recipient_phone="+39 02 1234567")
    _validate_share_create(body)


def test_anonymous_full_payload_is_accepted() -> None:
    _validate_share_create(_anon_payload())


def test_invalid_mode_is_rejected_at_pydantic() -> None:
    # The ``mode`` field has a Pydantic regex pattern, so the bad value
    # is caught before the validator runs. We assert the pattern exists
    # by trying to construct the model directly.
    with pytest.raises(Exception):
        ShareCreateIn(
            access_level="viewer",
            target=ShareTarget(kind="link_public"),
            mode="bogus",
        )


def test_autogen_password_length_and_alphabet() -> None:
    pwd = _autogen_password()
    assert len(pwd) == 24
    # Restricted alphabet: no ambiguous I / O / L / 0 / 1 characters.
    forbidden = set("IOl01")
    assert not (set(pwd) & forbidden)
    # Multiple calls must not collapse to the same value (entropy floor
    # check — not a proper randomness test, but catches a stuck PRNG).
    seen = {_autogen_password() for _ in range(10)}
    assert len(seen) == 10


def test_actor_context_link_kind_round_trip() -> None:
    import uuid

    sid = uuid.uuid4()
    link_id = uuid.uuid4()
    ctx = ActorContext(subject_id=sid, kind="link", share_link_id=link_id)
    assert ctx.kind == "link"
    assert ctx.share_link_id == link_id
    assert ctx.subject_id == sid
    # Default fields stay at None for the link branch.
    assert ctx.agent_token_id is None
    assert ctx.model_id is None
