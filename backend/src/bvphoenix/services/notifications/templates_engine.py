"""Jinja2 template loader + PII allowlist.

Templates live under ``services/notifications/templates/{locale}/{kind}.{slot}.jinja``
where slot is ``subject`` / ``txt`` / ``html``. The engine is built
with autoescape ON for ``html`` and OFF for ``txt`` / ``subject``
(plain-text envelopes don't need HTML escaping and look awful with
``&amp;`` etc.).

PII allowlist
-------------

The variable set passed to the template is intentionally narrow:

* ``patient_first_name`` — only the first name token, never the
  surname or codice fiscale
* ``event_title`` — the human label the user typed on the source
  event / task
* ``event_when_local`` — already-formatted "lun 15 giu 2026, 09:00"
  string; no raw timestamps that might leak timezone info we'd
  rather omit
* ``event_where`` — optional location string
* ``event_meeting_url`` — optional video-call URL
* ``app_url`` — link back to the fascicolo on the FE
* ``opt_out_url`` — single-click unsubscribe (always required)

Any variable not in :data:`ALLOWED_TEMPLATE_VARS` is rejected before
rendering with a ``ValueError``. This is the simplest form of "no
PII in the subject line by mistake": adding a new variable to the
template literally requires the developer to update the allowlist,
which forces a conscious decision about what's safe to ship.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Template, select_autoescape

# Whitelisted template variables. Adding to this set is a privacy
# decision — extend deliberately, NEVER pass arbitrary context.
ALLOWED_TEMPLATE_VARS: frozenset[str] = frozenset(
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

# Slot → template suffix. The engine renders one of each per
# notification (subject + body_text + optional body_html).
TEMPLATE_SLOTS: tuple[str, ...] = ("subject", "txt", "html")

# Supported notification kinds match NotificationKind. Validated at
# template-fetch time.
SUPPORTED_KINDS: frozenset[str] = frozenset({"event_reminder", "task_reminder", "followup"})

SUPPORTED_LOCALES: frozenset[str] = frozenset({"it", "en"})


def _templates_root() -> Path:
    return Path(__file__).resolve().parent / "templates"


@lru_cache(maxsize=1)
def _env_for(locale: str) -> Environment:
    root = _templates_root()
    if not root.is_dir():
        raise RuntimeError(f"notification templates root not found at {root}")
    return Environment(
        loader=FileSystemLoader(str(root / locale)),
        autoescape=select_autoescape(enabled_extensions=("html",)),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _resolve_locale(raw: str | None) -> str:
    if not raw:
        return "it"
    primary = raw.split(",")[0].split("-")[0].strip().lower()
    return primary if primary in SUPPORTED_LOCALES else "it"


def _check_allowlist(context: dict[str, object]) -> None:
    leaked = set(context.keys()) - ALLOWED_TEMPLATE_VARS
    if leaked:
        raise ValueError(
            f"notification template context contains non-allowlisted keys: {sorted(leaked)}"
        )


def _fetch_template(locale: str, kind: str, slot: str) -> Template:
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported notification kind: {kind}")
    if slot not in TEMPLATE_SLOTS:
        raise ValueError(f"unsupported template slot: {slot}")
    env = _env_for(locale)
    return env.get_template(f"{kind}.{slot}.jinja")


def render(
    *,
    kind: str,
    locale: str | None,
    context: dict[str, object],
) -> tuple[str, str, str | None]:
    """Render the three slots for a (kind, locale) pair. Returns
    ``(subject, body_text, body_html_or_none)``.

    ``opt_out_url`` is mandatory in ``context`` — the absence of an
    unsubscribe link is a deliverability + GDPR foot-gun, so we fail
    loud rather than silently emit an unsubscribe-less email.
    """
    _check_allowlist(context)
    if not context.get("opt_out_url"):
        raise ValueError(
            "notification template requires opt_out_url in context; refusing to render"
        )
    resolved = _resolve_locale(locale)

    subject_t = _fetch_template(resolved, kind, "subject")
    txt_t = _fetch_template(resolved, kind, "txt")
    subject = subject_t.render(**context).strip()
    body_text = txt_t.render(**context)
    body_html: str | None
    try:
        html_t = _fetch_template(resolved, kind, "html")
        body_html = html_t.render(**context)
    except Exception:
        # HTML slot is optional; absence falls back to text/plain.
        body_html = None
    return subject, body_text, body_html


__all__ = [
    "ALLOWED_TEMPLATE_VARS",
    "SUPPORTED_KINDS",
    "SUPPORTED_LOCALES",
    "render",
]
