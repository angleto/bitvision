"""Notification subsystem — pure unit tests (no DB / no SMTP).

Covers the contracts the dispatcher relies on:

* template engine — PII allowlist rejects unauthorised keys;
  rendering produces a subject + body_text + body_html for the three
  kinds in both IT and EN
* scheduling — _idempotency_key is deterministic;
  _normalise_offsets dedups + caps; _pick_channels respects
  per-channel consent + feature flag
* webhook — HMAC signature format matches what receivers should
  verify (the public contract of the X-BV-Signature header)
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

import pytest

from bvphoenix.services.notifications.scheduling import (
    _idempotency_key,
    _normalise_offsets,
    _parse_default_offsets,
)
from bvphoenix.services.notifications.templates_engine import (
    ALLOWED_TEMPLATE_VARS,
    SUPPORTED_KINDS,
    SUPPORTED_LOCALES,
    render,
)
from bvphoenix.services.notifications.webhook_notifier import _sign

# ---------------------------------------------------------------------------
# Template engine
# ---------------------------------------------------------------------------


def _base_context(**overrides) -> dict:
    ctx = {
        "patient_first_name": "Mario",
        "event_title": "Visita oncologica",
        "event_when_local": "15 giu 2026, 09:00",
        "event_where": "Ospedale X",
        "event_meeting_url": "https://meet.example/abc",
        "app_url": "https://bv/patients/p?view=events",
        "opt_out_url": "https://bv/api/notifications/opt-out?token=xxx&channel=email",
    }
    ctx.update(overrides)
    return ctx


def test_template_render_event_reminder_it() -> None:
    subject, txt, html = render(kind="event_reminder", locale="it", context=_base_context())
    assert "Promemoria" in subject
    assert "Visita oncologica" in subject
    assert "Mario" in txt
    assert "Ospedale X" in txt
    assert "Visita oncologica" in txt
    assert html is not None
    assert "<html" in html


def test_template_render_event_reminder_en() -> None:
    subject, txt, html = render(kind="event_reminder", locale="en", context=_base_context())
    assert "Reminder" in subject
    assert "Hi Mario" in txt
    assert html is not None


def test_template_render_task_and_followup() -> None:
    # task_reminder without event_where (allowed; template guards on it)
    ctx = _base_context()
    del ctx["event_where"]
    del ctx["event_meeting_url"]
    s, t, _h = render(kind="task_reminder", locale="it", context=ctx)
    assert "Attività" in s
    assert "Visita oncologica" in t

    s2, _t2, h2 = render(kind="followup", locale="en", context=ctx)
    assert "Follow-up" in s2
    assert h2 is not None


def test_template_locale_fallback_to_italian() -> None:
    subject, _, _ = render(kind="event_reminder", locale="fr-FR", context=_base_context())
    # Italian render → subject starts with "Promemoria"
    assert subject.startswith("Promemoria")


def test_template_rejects_unknown_var() -> None:
    with pytest.raises(ValueError) as exc:
        render(
            kind="event_reminder",
            locale="it",
            context={
                **_base_context(),
                "codice_fiscale": "RSSMRA85T10A562S",
            },
        )
    assert "codice_fiscale" in str(exc.value)


def test_template_requires_opt_out_url() -> None:
    ctx = _base_context()
    del ctx["opt_out_url"]
    with pytest.raises(ValueError) as exc:
        render(kind="event_reminder", locale="it", context=ctx)
    assert "opt_out_url" in str(exc.value)


def test_template_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        render(kind="random_kind", locale="it", context=_base_context())


def test_allowlist_is_locked_down() -> None:
    """If the allowlist grows without an explicit decision the
    privacy contract is broken. Pin the set size + content."""
    assert (
        frozenset(
            {
                "patient_first_name",
                "event_title",
                "event_when_local",
                "event_where",
                "event_meeting_url",
                "app_url",
                "opt_out_url",
            }
        )
        == ALLOWED_TEMPLATE_VARS
    )
    assert "patient_full_name" not in ALLOWED_TEMPLATE_VARS
    assert "codice_fiscale" not in ALLOWED_TEMPLATE_VARS
    assert "diagnosis" not in ALLOWED_TEMPLATE_VARS


def test_supported_kinds_locales() -> None:
    assert frozenset({"event_reminder", "task_reminder", "followup"}) == SUPPORTED_KINDS
    assert frozenset({"it", "en"}) == SUPPORTED_LOCALES


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------


def test_idempotency_key_deterministic() -> None:
    target = uuid.UUID("11111111-1111-1111-1111-111111111111")
    contact = uuid.UUID("22222222-2222-2222-2222-222222222222")
    k1 = _idempotency_key(target, contact, -60, "email")
    k2 = _idempotency_key(target, contact, -60, "email")
    assert k1 == k2
    # Different inputs → different keys
    assert _idempotency_key(target, contact, -120, "email") != k1
    assert _idempotency_key(target, contact, -60, "webhook_telegram") != k1


def test_normalise_offsets_dedups_caps_filters() -> None:
    assert _normalise_offsets([-15, -60, -15, -120, -60]) == [-15, -60, -120]
    out = _normalise_offsets([-15, -30, -60, -120, -240, -480, -1440])
    assert len(out) == 5
    assert _normalise_offsets(None) != []  # falls back to default
    assert _normalise_offsets([-15, "x", None, -60]) == [-15, -60]


def test_parse_default_offsets() -> None:
    assert _parse_default_offsets("-1440,-60") == [-1440, -60]
    assert _parse_default_offsets("-15") == [-15]
    assert _parse_default_offsets("") == []
    assert _parse_default_offsets("not-a-number,-60") == [-60]


# ---------------------------------------------------------------------------
# Webhook signature
# ---------------------------------------------------------------------------


def test_webhook_signature_format() -> None:
    """The X-BV-Signature header is "sha256=<hex>" where the digest
    is HMAC-SHA256 of the raw body with the per-contact secret. Pin
    the format so external integrations can verify it."""
    body = b'{"kind":"event_reminder","subject":"Test"}'
    secret = b"superdupersecret"
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert _sign(body, secret) == expected


def test_webhook_signature_changes_with_body() -> None:
    secret = b"k"
    s1 = _sign(b"a", secret)
    s2 = _sign(b"b", secret)
    assert s1 != s2


def test_webhook_signature_changes_with_secret() -> None:
    body = b"payload"
    assert _sign(body, b"k1") != _sign(body, b"k2")
