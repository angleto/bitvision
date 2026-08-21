"""Pluggable email sender + transactional email builders.

Two backends selected by configuration:

* **Dev** (when ``BVP_SMTP_HOST`` is empty, or ``BVP_EMAIL_PROVIDER``
  is explicitly ``stub`` / ``log``) — writes the RFC 822 message to
  ``logs/dev_emails.eml`` and prints it to stdout. Visible in docker
  logs. No external dependencies.
* **Prod** — stdlib ``smtplib``, with the transport security mode
  chosen by ``BVP_SMTP_SECURITY`` (``starttls`` / ``implicit`` /
  ``none``).

Delivery failure is **observable**. The transport raises
:class:`EmailDeliveryError`, carrying a discriminated ``error_code``
and a ``retriable`` flag, and the two public entry points differ only
in how they surface it:

* :func:`send_email` — ``async``; offloads the blocking socket work to
  a worker thread and lets :class:`EmailDeliveryError` propagate.
* :func:`send_email_sync` — returns a :class:`DeliveryOutcome` instead
  of raising, for callers that persist the failure rather than bubble
  it (the notifier layer and the delivery ledger).

Historical note, because it cost an outage: this module used to expose
helpers that "never raise" and returned ``None`` either way. Callers
had no value to inspect, so a share-link notify endpoint reported HTTP
200 and wrote a ``share_email_sent`` audit row for messages that never
left the pod. Swallowing a transport error is a decision for the call
site to make explicitly, never a property of the transport.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import smtplib
import socket
import ssl
import sys
from dataclasses import dataclass
from email.message import EmailMessage as StdlibEmailMessage
from pathlib import Path
from typing import Protocol

from bvphoenix.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEV_EMAIL_LOG_PATH = Path("logs/dev_emails.eml")

# Discriminated failure codes. These are persisted on the delivery
# ledger and echoed to API clients, so they are part of the contract:
# add cases, don't repurpose existing ones.
ERROR_CONNECT_TIMEOUT = "smtp_connect_timeout"
ERROR_UNREACHABLE = "smtp_unreachable"
ERROR_CONNECT_FAILED = "smtp_connect_failed"
ERROR_DNS = "smtp_dns_failure"
ERROR_AUTH = "smtp_auth_failed"
ERROR_RECIPIENT_REFUSED = "smtp_recipient_refused"
ERROR_SENDER_REFUSED = "smtp_sender_refused"
ERROR_TLS = "smtp_tls_failed"
ERROR_INSECURE_AUTH = "smtp_insecure_auth_refused"
ERROR_DISCONNECTED = "smtp_server_disconnected"
ERROR_NOT_CONFIGURED = "smtp_not_configured"
ERROR_UNKNOWN = "smtp_unknown_error"

# Codes worth retrying. A refused recipient or a rejected credential
# will be refused identically on the next attempt; a dropped packet or
# a disconnect will not.
_RETRIABLE_CODES: frozenset[str] = frozenset(
    {
        ERROR_CONNECT_TIMEOUT,
        ERROR_UNREACHABLE,
        ERROR_CONNECT_FAILED,
        ERROR_DNS,
        ERROR_DISCONNECTED,
        ERROR_UNKNOWN,
    }
)


class EmailDeliveryError(Exception):
    """A message could not be handed to the relay.

    ``error_code`` is one of the ``ERROR_*`` constants above.
    ``detail`` is operator-facing and may name hosts, ports and
    resolved addresses; it must never carry message bodies.
    """

    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(f"{error_code}: {detail}")
        self.error_code = error_code
        self.detail = detail

    @property
    def retriable(self) -> bool:
        return self.error_code in _RETRIABLE_CODES


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """Result of a non-raising send attempt."""

    ok: bool
    error_code: str | None = None
    error_detail: str | None = None
    retriable: bool = False

    @classmethod
    def success(cls) -> DeliveryOutcome:
        return cls(ok=True)

    @classmethod
    def from_error(cls, exc: EmailDeliveryError) -> DeliveryOutcome:
        return cls(
            ok=False,
            error_code=exc.error_code,
            error_detail=exc.detail,
            retriable=exc.retriable,
        )


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


def _describe_targets(host: str, port: int) -> str:
    """Best-effort rendering of what ``host`` resolves to.

    Purely diagnostic: any failure here must not mask the delivery
    error we are in the middle of reporting.
    """
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except Exception:
        return "unresolved"
    seen: list[str] = []
    for info in infos:
        family, sockaddr = info[0], info[4]
        label = "IPv6" if family == socket.AF_INET6 else "IPv4"
        rendered = f"{label} {sockaddr[0]}"
        if rendered not in seen:
            seen.append(rendered)
    return ", ".join(seen) or "unresolved"


def _connect_failure(host: str, port: int, excs: list[BaseException]) -> EmailDeliveryError:
    """Turn every failed connect attempt into one discriminated error."""
    rendered = "; ".join(f"{type(e).__name__}: {e}" for e in excs) or "no detail"
    detail = f"connect to {host}:{port} failed [{_describe_targets(host, port)}] -> {rendered}"
    # A timeout outranks an unreachable: when a host resolves to both a
    # v4 and a v6 address on a v4-only pod, the v6 leg is *expected* to
    # be unreachable and says nothing. The v4 timeout is the real signal
    # and the one an operator needs to see first.
    if any(isinstance(e, TimeoutError) for e in excs):
        return EmailDeliveryError(ERROR_CONNECT_TIMEOUT, detail)
    unreachable = {errno.ENETUNREACH, errno.EHOSTUNREACH}
    if excs and all(isinstance(e, OSError) and e.errno in unreachable for e in excs):
        return EmailDeliveryError(ERROR_UNREACHABLE, detail)
    return EmailDeliveryError(ERROR_CONNECT_FAILED, detail)


def _diagnostic_connect(host: str, port: int, timeout: float, source: object) -> socket.socket:
    """``create_connection`` that reports *every* address it tried.

    ``socket.create_connection`` discards all but the last exception
    unless ``all_errors=True``, and ``smtplib`` never passes the flag.
    For a dual-stack host reached from a v4-only pod that means the
    surfaced error is the trailing IPv6 ``ENETUNREACH`` while the real
    blocker is the IPv4 timeout — exactly backwards for diagnosis, and
    the reason the 2026-07-31 outage read as an IPv6 routing problem.
    """
    try:
        return socket.create_connection((host, port), timeout, source, all_errors=True)  # type: ignore[arg-type]
    except ExceptionGroup as eg:
        raise _connect_failure(host, port, list(eg.exceptions)) from eg


class _DiagnosticSMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):  # type: ignore[no-untyped-def]
        return _diagnostic_connect(host, port, timeout, self.source_address)


class _DiagnosticSMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):  # type: ignore[no-untyped-def]
        # Mirrors smtplib.SMTP_SSL._get_socket, but over our connect.
        # Upstream reads ``self._host``; the ``host`` argument carries
        # the same value and is visible to the type checker.
        raw = _diagnostic_connect(host, port, timeout, self.source_address)
        return self.context.wrap_socket(raw, server_hostname=host)


def _classify(exc: BaseException, host: str, port: int) -> EmailDeliveryError:
    """Map a transport exception onto a discriminated delivery error."""
    if isinstance(exc, EmailDeliveryError):
        return exc
    where = f"{host}:{port}"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return EmailDeliveryError(ERROR_AUTH, f"{where} rejected the credentials: {exc}")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return EmailDeliveryError(ERROR_RECIPIENT_REFUSED, f"{where} refused every recipient")
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return EmailDeliveryError(
            ERROR_SENDER_REFUSED, f"{where} refused the envelope sender: {exc}"
        )
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return EmailDeliveryError(ERROR_DISCONNECTED, f"{where} dropped the connection: {exc}")
    if isinstance(exc, ssl.SSLError):
        return EmailDeliveryError(ERROR_TLS, f"TLS handshake with {where} failed: {exc}")
    if isinstance(exc, socket.gaierror):
        return EmailDeliveryError(ERROR_DNS, f"cannot resolve {host}: {exc}")
    if isinstance(exc, TimeoutError):
        return EmailDeliveryError(ERROR_CONNECT_TIMEOUT, f"{where} timed out: {exc}")
    if isinstance(exc, OSError) and exc.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
        return EmailDeliveryError(ERROR_UNREACHABLE, f"{where} unreachable: {exc}")
    if isinstance(exc, OSError):
        return EmailDeliveryError(ERROR_CONNECT_FAILED, f"{where}: {exc}")
    return EmailDeliveryError(ERROR_UNKNOWN, f"{where}: {type(exc).__name__}: {exc}")


class SmtpEmailSender:
    """SMTP client with an explicit transport-security mode.

    Raises :class:`EmailDeliveryError` on any failure. It never returns
    a partial success: either the relay accepted the message, or the
    caller gets a code it can act on.
    """

    def __init__(self, settings: Settings) -> None:
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username
        self.password = settings.smtp_password
        self.security = settings.resolved_smtp_security
        self.timeout = settings.smtp_timeout_seconds

    def send(self, message: StdlibEmailMessage) -> None:
        try:
            self._send(message)
        except EmailDeliveryError:
            raise
        except Exception as exc:
            raise _classify(exc, self.host, self.port) from exc

    def _send(self, message: StdlibEmailMessage) -> None:
        context = ssl.create_default_context()
        if self.security == "implicit":
            client: smtplib.SMTP = _DiagnosticSMTP_SSL(
                self.host, self.port, timeout=self.timeout, context=context
            )
        else:
            client = _DiagnosticSMTP(self.host, self.port, timeout=self.timeout)
        with client as smtp:
            smtp.ehlo()
            if self.security == "starttls":
                smtp.starttls(context=context)
                # RFC 3207: the session resets on upgrade, so the
                # capability list has to be re-fetched before AUTH.
                smtp.ehlo()
            if self.username:
                if self.security == "none":
                    # Refuse rather than leak. The credential is a TEM
                    # API token and the body of a share invitation
                    # carries a landing URL that grants access to PHI;
                    # neither may cross the wire in the clear.
                    raise EmailDeliveryError(
                        ERROR_INSECURE_AUTH,
                        f"refusing to authenticate to {self.host}:{self.port} "
                        "over an unencrypted channel (smtp_security='none')",
                    )
                smtp.login(self.username, self.password)
            smtp.send_message(message)


def _get_sender(settings: Settings | None = None) -> EmailSender:
    settings = settings or get_settings()
    # ``stub`` / ``log`` are an explicit opt-out, honoured even when a
    # host is configured — tests use it to pin the dev sender against
    # environment leakage. It is NOT the default: making it so meant a
    # single missing ConfigMap key could divert all production mail
    # into a file inside an ephemeral pod, reporting success.
    provider = (getattr(settings, "email_provider", "") or "").lower().strip()
    if provider in ("stub", "log"):
        return DevEmailSender()
    if settings.smtp_host:
        return SmtpEmailSender(settings)
    if provider == "smtp":
        # Asked for a real relay and none is configured. Failing loudly
        # beats writing the message to a file and calling it sent.
        raise EmailDeliveryError(
            ERROR_NOT_CONFIGURED,
            "BVP_EMAIL_PROVIDER=smtp but BVP_SMTP_HOST is empty",
        )
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


def _deliver(message: EmailMessage) -> None:
    """Blocking send. Raises :class:`EmailDeliveryError` on failure."""
    settings = get_settings()
    try:
        sender = _get_sender(settings)
        sender.send(_to_stdlib(message, settings))
    except EmailDeliveryError:
        raise
    except Exception as exc:
        raise _classify(exc, settings.smtp_host or "(dev sender)", settings.smtp_port) from exc


async def send_email(message: EmailMessage) -> None:
    """Dispatch an email, raising :class:`EmailDeliveryError` on failure.

    The socket work runs in a worker thread. It used to run inline on
    the event loop, which meant one blocked relay froze the whole
    uvicorn worker — including ``/health`` — for the connect timeout,
    since the backend image starts uvicorn without ``--workers``.

    Callers that must not fail on a flaky mailer (the auth flows, which
    stay deliberately uninformative to resist account enumeration) are
    expected to catch this explicitly and record the failure, not to
    rely on the transport silently absorbing it.
    """
    await asyncio.to_thread(_deliver, message)


def send_email_sync(message: EmailMessage) -> DeliveryOutcome:
    """Blocking send that reports rather than raises.

    Returns a :class:`DeliveryOutcome` carrying the discriminated
    ``error_code``, the operator-facing ``error_detail`` and whether a
    retry could plausibly succeed. Used by the delivery ledger and the
    notifier layer, which persist the failure instead of propagating it.
    """
    try:
        _deliver(message)
        return DeliveryOutcome.success()
    except EmailDeliveryError as exc:
        logger.error(
            "email delivery failed to %s (subject=%r) code=%s: %s",
            message.to,
            message.subject,
            exc.error_code,
            exc.detail,
        )
        return DeliveryOutcome.from_error(exc)


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


def normalize_locale(value: str | None) -> str:
    """Public alias for :func:`_normalize_locale`.

    Call sites outside this module (the sharing API resolves the locale
    before building an invitation) should not have to reach for a
    private name.
    """
    return _normalize_locale(value)


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


VERIFICATION_SUBJECT = "Verify your bitvision phoenix email"

VERIFICATION_BODY = (
    "Welcome to bitvision phoenix.\n\n"
    "Please confirm your email address by visiting the link below. "
    "The link expires in 24 hours and can only be used once.\n\n"
    "{token_url}\n\n"
    "If you didn't create this account, you can ignore this message."
)


def build_verification_email(*, to: str, token_url: str) -> EmailMessage:
    """Verification link as an :class:`EmailMessage`.

    The older ``_build_verification_message`` renders straight to a
    stdlib message, which the delivery ledger cannot carry. Same copy,
    expressed in the dataclass every other transactional email uses.
    """
    return EmailMessage(
        to=to,
        subject=VERIFICATION_SUBJECT,
        body_text=VERIFICATION_BODY.format(token_url=token_url),
    )


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
) -> DeliveryOutcome:
    """Send the verification link to ``email``.

    Reports rather than raises: registration must not 5xx because the
    relay is down. The caller is expected to have queued a ledger row
    first, so a failure here is retried rather than lost — without that
    row an outage turns every signup into a permanent lockout, since
    ``email_verified_at`` has no other write path.
    """
    settings = settings or get_settings()
    try:
        sender = _get_sender(settings)
        sender.send(_build_verification_message(email, token_url, settings))
        return DeliveryOutcome.success()
    except EmailDeliveryError as exc:
        # phi-safe: PHIRedactionFilter scrubs the email at format time
        logger.error(
            "verification email to %s failed code=%s: %s", email, exc.error_code, exc.detail
        )
        return DeliveryOutcome.from_error(exc)
