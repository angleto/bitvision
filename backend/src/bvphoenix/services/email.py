"""Pluggable email sender + transactional email builders.

Two backends selected by configuration:

* **Dev** (default when ``BVP_SMTP_HOST`` is empty OR
  ``BVP_EMAIL_PROVIDER=stub``) — writes the RFC 822 message to
  ``logs/dev_emails.eml`` and prints it to stdout. Visible in docker
  logs. No external dependencies.
* **Prod** — stdlib ``smtplib`` with STARTTLS when
  ``BVP_SMTP_USE_TLS`` is true.

The module exposes two coroutine-free helpers that never raise — a
flaky mailer must not take down the auth flow:

* :func:`send_verification_email` — for the registration flow
* :func:`send_email` + :func:`build_password_reset_email` — for the
  password-reset flow

Both internally route through the same ``_get_sender`` dispatch.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import sys
from dataclasses import dataclass
from email.message import EmailMessage as StdlibEmailMessage
from pathlib import Path
from typing import Protocol

from bvphoenix.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEV_EMAIL_LOG_PATH = Path("logs/dev_emails.eml")


class EmailSender(Protocol):
    def send(self, message: StdlibEmailMessage) -> None:  # pragma: no cover - protocol
        ...


class DevEmailSender:
    """File-based sender used when no SMTP host is configured."""

    def __init__(self, log_path: Path = DEV_EMAIL_LOG_PATH) -> None:
        self.log_path = log_path

    def send(self, message: StdlibEmailMessage) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab") as fh:
            fh.write(b"\n----- BEGIN DEV EMAIL -----\n")
            fh.write(message.as_bytes())
            fh.write(b"\n----- END DEV EMAIL -----\n")
        # ``message.get_content()`` raises ``KeyError`` on multipart
        # roots (the content manager only ships handlers for the leaf
        # text/* and application/* types). For dev logging we just want
        # the plain-text body, which lives on the first ``text/plain``
        # part; iter_parts() walks the tree safely.
        preview = ""
        try:
            if message.is_multipart():
                for part in message.walk():
                    if part.get_content_type() == "text/plain":
                        preview = part.get_content()
                        break
            else:
                preview = message.get_content()
        except Exception:
            preview = "(multipart preview unavailable)"
        sys.stdout.write(
            f"[dev-email] to={message['To']} subject={message['Subject']}\n{preview}\n"
        )
        sys.stdout.flush()


class SmtpEmailSender:
    """Minimal SMTP client — STARTTLS + optional auth."""

    def __init__(self, settings: Settings) -> None:
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username
        self.password = settings.smtp_password
        self.use_tls = settings.smtp_use_tls

    def send(self, message: StdlibEmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
            smtp.ehlo()
            if self.use_tls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)


def _get_sender(settings: Settings | None = None) -> EmailSender:
    settings = settings or get_settings()
    # Honour BVP_EMAIL_PROVIDER=stub as an explicit opt-out even when
    # BVP_SMTP_HOST is set — useful in tests that want to pin the dev
    # sender regardless of environment leakage.
    provider = (getattr(settings, "email_provider", "") or "").lower().strip()
    if provider in ("stub", "log"):
        return DevEmailSender()
    if settings.smtp_host:
        return SmtpEmailSender(settings)
    return DevEmailSender()


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    """One MIME attachment carried alongside an EmailMessage.

    Used by the notification dispatcher (sprint C3) to attach the
    one-shot .ics file to an event / task reminder so the recipient's
    calendar app imports it directly from the email — no separate
    download step. ``filename`` ends up as ``Content-Disposition:
    attachment; filename=...``; ``mime_type`` is split on ``/`` into
    maintype/subtype for the stdlib EmailMessage API."""

    filename: str
    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """High-level message used by call sites that don't care about MIME."""

    to: str
    subject: str
    body_text: str
    body_html: str | None = None
    # Per-message extras (sprint C). All optional so existing call
    # sites (password reset, share invitation, verification) keep
    # their current shape.
    attachments: tuple[EmailAttachment, ...] = ()
    reply_to: str | None = None
    # RFC 8058 one-click unsubscribe pair. When both are set the
    # email client surfaces a native "Unsubscribe" button — much
    # higher signal-to-spam ratio than a plain footer link.
    list_unsubscribe_url: str | None = None
    list_unsubscribe_post_url: str | None = None
    # Extra headers (e.g. ``Precedence: bulk``, ``Auto-Submitted:
    # auto-generated``). Kept as a tuple of (name, value) so the
    # frozen dataclass stays hashable and immutable.
    extra_headers: tuple[tuple[str, str], ...] = ()


def _to_stdlib(msg: EmailMessage, settings: Settings) -> StdlibEmailMessage:
    out = StdlibEmailMessage()
    out["Subject"] = msg.subject
    out["From"] = (
        f"{settings.smtp_from_name} <{settings.smtp_from_address}>"
        if settings.smtp_from_name
        else settings.smtp_from_address
    )
    out["To"] = msg.to
    if msg.reply_to:
        out["Reply-To"] = msg.reply_to
    if msg.list_unsubscribe_url:
        # RFC 2369 / RFC 8058. Mail clients render an "Unsubscribe"
        # affordance when ``List-Unsubscribe`` is present; the
        # ``List-Unsubscribe-Post`` header opts into the one-click
        # POST form (RFC 8058 §4).
        out["List-Unsubscribe"] = f"<{msg.list_unsubscribe_url}>"
        if msg.list_unsubscribe_post_url:
            out["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    for name, value in msg.extra_headers:
        out[name] = value
    # Body assembly depends on what's present:
    #
    # 1. Plain text only            → set_content(text)
    # 2. Plain text + HTML          → set_content + add_alternative
    # 3. Plain text + attachments   → set_content + add_attachment ×N
    # 4. Plain text + HTML + attachments
    #    → set_content + add_attachment ×N + add_alternative
    #
    # Case 4 is the load-bearing one: stdlib EmailMessage's content
    # manager raises ``KeyError('multipart/mixed')`` if HTML is added
    # BEFORE attachments because the resulting tree (text+html-alt)
    # can't be promoted to mixed cleanly. Adding attachments first
    # gives the root a chance to become multipart/mixed, with the
    # text body as its first part; the HTML alternative then sits as
    # a sibling of the text part.
    out.set_content(msg.body_text)
    for att in msg.attachments:
        mime = att.mime_type.split(";", 1)[0].strip()
        try:
            maintype, subtype = mime.split("/", 1)
        except ValueError:
            maintype, subtype = "application", "octet-stream"
        out.add_attachment(
            att.data,
            maintype=maintype,
            subtype=subtype,
            filename=att.filename,
        )
    if msg.body_html:
        # When attachments are present the root is multipart/mixed
        # and add_alternative will not climb the tree correctly —
        # the HTML must be inserted as an alternative to the text
        # part of the mixed root. Detect that case and use the
        # iter_parts() walk to find the text/plain part directly.
        if msg.attachments:
            text_part = None
            for part in out.iter_parts():
                if part.get_content_type() == "text/plain":
                    text_part = part
                    break
            if text_part is not None:
                text_part.add_alternative(msg.body_html, subtype="html")
            # If no text part somehow exists (shouldn't happen since
            # we always set_content above) we silently drop the HTML
            # alternative — the recipient still gets the plain body.
        else:
            out.add_alternative(msg.body_html, subtype="html")
    return out


async def send_email(message: EmailMessage) -> None:
    """Dispatch an email. Never raises."""
    settings = get_settings()
    try:
        sender = _get_sender(settings)
        sender.send(_to_stdlib(message, settings))
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to send email to %s (subject=%r)", message.to, message.subject)


def send_email_sync(message: EmailMessage) -> bool:
    """Synchronous variant. Returns True on success, False on any
    failure. Used by the arq worker dispatcher which doesn't want to
    fan out the exception (a failed delivery becomes
    ``notification_dispatches.status='failed'`` row, not a worker
    crash). Logs at error level on failure with PHI-safe redaction."""
    settings = get_settings()
    try:
        sender = _get_sender(settings)
        sender.send(_to_stdlib(message, settings))
        return True
    except Exception:
        logger.exception("failed to send email to %s (subject=%r)", message.to, message.subject)
        return False


def build_password_reset_email(*, to: str, reset_url: str, ttl_minutes: int) -> EmailMessage:
    """Format the password-reset email body.

    The copy doesn't confirm whether the address has an account —
    leakage happens at the message *content* level, not just HTTP.
    """
    text = (
        "A password reset was requested for this email address on bitvision phoenix.\n"
        f"\nIf this was you, open the following link within {ttl_minutes} minutes:\n"
        f"\n    {reset_url}\n"
        "\nIf you did not request a reset, you can safely ignore this message —\n"
        "no change has been made to your account.\n"
    )
    return EmailMessage(to=to, subject="bitvision phoenix password reset", body_text=text)


SUPPORTED_EMAIL_LOCALES: tuple[str, ...] = ("it", "en")


def _normalize_locale(value: str | None) -> str:
    """Pick a supported locale from a free-form value (cookie, header).

    The platform speaks IT first and EN second; everything else falls
    back to IT. Done at the boundary so every caller of
    :func:`build_share_invitation_email` can stay locale-agnostic.
    """
    if not value:
        return "it"
    primary = value.split(",")[0].split("-")[0].strip().lower()
    return primary if primary in SUPPORTED_EMAIL_LOCALES else "it"


def build_share_invitation_email(
    *,
    to: str,
    recipient_name: str | None,
    grantor_name: str,
    study_summary: str,
    landing_url: str,
    expires_label: str,
    deidentified: bool,
    autogen_password: str | None = None,
    custom_message: str | None = None,
    locale: str | None = "it",
) -> EmailMessage:
    """Format the "study shared by X" email in the requested locale.

    The body is intentionally human (not a marketing template) so a
    consultant who receives it doesn't dismiss it as phishing. We
    state the grantor by name, the study by its description (which
    is *not* PHI when ``deidentified`` is True — PatientName /
    PatientID are scrubbed at download time), and what they're
    allowed to do.

    The autogenerated password, if any, is included in a separate
    paragraph with an explicit "delivered separately" note so the
    sender is nudged toward an out-of-band channel for it; the
    server's email itself isn't the right place to put it.

    ``locale`` controls IT / EN copy. The recipient's preferred
    language isn't known to the backend, so callers pass the
    *grantor's* locale (read from the request) — best effort and
    overridable on a per-recipient basis later if we add a
    ``preferred_locale`` field to PatientContact.
    """
    lang = _normalize_locale(locale)
    custom_block = f"\n{custom_message.strip()}\n\n" if custom_message else "\n"
    if lang == "en":
        salutation = f"Dear {recipient_name},\n\n" if recipient_name else "Hello,\n\n"
        deid_line = (
            "The images are pseudonymized (PS3.15 Basic Profile): "
            "DICOM identifying tags removed or substituted."
            if deidentified
            else "The images carry the original identifying metadata."
        )
        pwd_block = (
            (
                "\nThe link is password-protected. The password will be sent\n"
                "to you separately (SMS / direct message) — never in the same\n"
                "email, for security reasons.\n"
            )
            if autogen_password
            else ""
        )
        body = (
            f"{salutation}"
            f"{grantor_name} has shared a DICOM study with you via bitvision phoenix.\n"
            f"{custom_block}"
            f"Study: {study_summary}\n"
            f"Valid until: {expires_label}\n"
            f"{deid_line}\n"
            "\nOpen the link to view the details, download the DICOM archive\n"
            "and confirm receipt:\n"
            f"\n    {landing_url}\n"
            f"{pwd_block}"
            "\nbitvision phoenix is a clinical sharing platform with full audit\n"
            "trail. Every access to this link is logged for clinical traceability.\n"
            "If you weren't expecting this message, ignore it: the link expires\n"
            "by itself at the date above.\n"
        )
        suffix = "(pseudonymized)" if deidentified else ""
        subject = f"DICOM study shared by {grantor_name} {suffix}".strip()
    else:
        salutation = f"Caro/a {recipient_name},\n\n" if recipient_name else "Salve,\n\n"
        deid_line = (
            "Le immagini sono pseudonimizzate (PS3.15 Basic Profile): "
            "tag identificativi DICOM rimossi o sostituiti."
            if deidentified
            else "Le immagini contengono i metadati identificativi originali."
        )
        pwd_block = (
            (
                "\nIl link è protetto da password. La password ti verrà inviata\n"
                "separatamente (SMS / messaggio diretto) — non includerla nella stessa\n"
                "email per ragioni di sicurezza.\n"
            )
            if autogen_password
            else ""
        )
        body = (
            f"{salutation}"
            f"{grantor_name} ha condiviso con te uno studio DICOM tramite bitvision phoenix.\n"
            f"{custom_block}"
            f"Studio: {study_summary}\n"
            f"Validità: {expires_label}\n"
            f"{deid_line}\n"
            "\nApri il link per consultare i dettagli, scaricare l'archivio DICOM\n"
            "e confermare la ricezione:\n"
            f"\n    {landing_url}\n"
            f"{pwd_block}"
            "\nbitvision phoenix è una piattaforma di condivisione clinica con audit\n"
            "trail. Ogni accesso è registrato a fini di tracciabilità sanitaria.\n"
            "Se non ti aspettavi questo messaggio, ignoralo: il link scade da solo\n"
            "alla data sopra indicata.\n"
        )
        suffix = "(pseudonimizzato)" if deidentified else ""
        subject = f"Studio DICOM condiviso da {grantor_name} {suffix}".strip()
    return EmailMessage(to=to, subject=subject, body_text=body)


def _build_verification_message(
    to_email: str, token_url: str, settings: Settings
) -> StdlibEmailMessage:
    msg = StdlibEmailMessage()
    msg["Subject"] = "Verify your bitvision phoenix email"
    msg["From"] = (
        f"{settings.smtp_from_name} <{settings.smtp_from_address}>"
        if settings.smtp_from_name
        else settings.smtp_from_address
    )
    msg["To"] = to_email
    msg.set_content(
        "Welcome to bitvision phoenix.\n\n"
        "Please confirm your email address by visiting the link below. "
        "The link expires in 24 hours and can only be used once.\n\n"
        f"{token_url}\n\n"
        "If you didn't create this account, you can ignore this message."
    )
    return msg


def send_verification_email(
    email: str, token_url: str, *, settings: Settings | None = None
) -> None:
    """Send the verification link to ``email``. Never raises."""
    settings = settings or get_settings()
    try:
        sender = _get_sender(settings)
        sender.send(_build_verification_message(email, token_url, settings))
    except Exception:  # pragma: no cover - defensive
        # phi-safe: PHIRedactionFilter scrubs the email at format time
        logger.exception("failed to send verification email to %s", email)
