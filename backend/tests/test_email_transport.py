"""Regression tests for the SMTP transport layer.

These exist because of the 2026-07-31 outbound-email outage. The
proximate cause was a Scaleway-blackholed submission port, but the
*defect* was that nothing observed the failure: the transport helpers
returned ``None`` whether or not the message left the pod, so
``POST /api/share-links/{id}/notify`` answered HTTP 200 with a
hard-coded ``sent: true`` for mail that was never delivered.

The contract asserted here is therefore narrow and load-bearing:

* every transport exception maps to a *discriminated* ``error_code``
  with a correct ``retriable`` flag (a refused credential must not be
  retried forever; a dropped packet must be);
* a connect timeout outranks a network-unreachable when several
  addresses were tried, because the dual-stack pod's IPv6 leg is
  *expected* to fail and reporting it first sent the original
  diagnosis in exactly the wrong direction;
* :func:`send_email` raises and :func:`send_email_sync` reports — one
  of them must always give the caller a value to inspect;
* the sender is never allowed to authenticate in the clear;
* the provider default resolves to real SMTP when a host is set, so a
  missing ConfigMap key cannot divert production mail into a file.

Everything runs against fakes: no socket is opened and no name is
resolved, so the suite is hermetic and fast.
"""

from __future__ import annotations

import errno
import smtplib
import socket
import ssl
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage as StdlibEmailMessage
from typing import Any

import pytest

from bvphoenix.config import Settings
from bvphoenix.services import email as email_mod
from bvphoenix.services.email import (
    ERROR_AUTH,
    ERROR_CONNECT_FAILED,
    ERROR_CONNECT_TIMEOUT,
    ERROR_DISCONNECTED,
    ERROR_DNS,
    ERROR_INSECURE_AUTH,
    ERROR_NOT_CONFIGURED,
    ERROR_RECIPIENT_REFUSED,
    ERROR_SENDER_REFUSED,
    ERROR_TLS,
    ERROR_UNKNOWN,
    ERROR_UNREACHABLE,
    DeliveryOutcome,
    DevEmailSender,
    EmailDeliveryError,
    EmailMessage,
    SmtpEmailSender,
    _classify,
    _connect_failure,
    _describe_targets,
    _diagnostic_connect,
    _get_sender,
    send_email,
    send_email_sync,
)

HOST = "smtp.relay.test"
PORT = 2587


def _settings(**overrides: Any) -> Settings:
    """Settings with every transport-relevant field pinned.

    ``Settings`` reads ``.env`` files and ``BVP_*`` environment
    variables; these tests assert on provider selection, so nothing may
    be left to ambient configuration. Callers override only the field
    under test.
    """
    base: dict[str, Any] = {
        "smtp_host": HOST,
        "smtp_port": PORT,
        "smtp_username": "",
        "smtp_password": "",
        "smtp_security": "starttls",
        "smtp_use_tls": True,
        "smtp_timeout_seconds": 7,
        "smtp_from_address": "no-reply@bitvision.test",
        "smtp_from_name": "bitvision phoenix",
        "email_provider": "",
    }
    base.update(overrides)
    return Settings(**base)


def _message(to: str = "consultant@example.test") -> EmailMessage:
    return EmailMessage(
        to=to,
        subject="Studio DICOM condiviso",
        body_text="https://app.test/s/abc123\n",
    )


# --------------------------------------------------------------------
# Fake SMTP client
# --------------------------------------------------------------------


@dataclass
class _SmtpTranscript:
    """Everything ``SmtpEmailSender._send`` did to its client."""

    kind: str | None = None
    host: str | None = None
    port: int | None = None
    timeout: float | None = None
    tls_context: Any = None
    calls: list[str] = field(default_factory=list)
    logins: list[tuple[str, str]] = field(default_factory=list)
    sent: list[StdlibEmailMessage] = field(default_factory=list)
    entered: int = 0
    exited: int = 0


def _install_fake_smtp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_on_send: BaseException | None = None,
    raise_on_login: BaseException | None = None,
) -> _SmtpTranscript:
    """Replace both diagnostic smtplib subclasses with recorders."""
    transcript = _SmtpTranscript()

    def _factory(label: str) -> type:
        class _FakeClient:
            def __init__(
                self,
                host: str,
                port: int,
                timeout: float | None = None,
                context: Any = None,
            ) -> None:
                transcript.kind = label
                transcript.host = host
                transcript.port = port
                transcript.timeout = timeout
                transcript.tls_context = context

            def __enter__(self) -> _FakeClient:
                transcript.entered += 1
                return self

            def __exit__(self, *exc_info: object) -> bool:
                transcript.exited += 1
                return False

            def ehlo(self) -> None:
                transcript.calls.append("ehlo")

            def starttls(self, context: Any = None) -> None:
                transcript.calls.append("starttls")

            def login(self, username: str, password: str) -> None:
                transcript.calls.append("login")
                transcript.logins.append((username, password))
                if raise_on_login is not None:
                    raise raise_on_login

            def send_message(self, message: StdlibEmailMessage) -> None:
                transcript.calls.append("send_message")
                if raise_on_send is not None:
                    raise raise_on_send
                transcript.sent.append(message)

        return _FakeClient

    monkeypatch.setattr(email_mod, "_DiagnosticSMTP", _factory("plain"))
    monkeypatch.setattr(email_mod, "_DiagnosticSMTP_SSL", _factory("implicit"))
    return transcript


class _RecordingSender:
    """``EmailSender`` that records, and optionally explodes."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.sent: list[StdlibEmailMessage] = []
        self.threads: list[int] = []

    def send(self, message: StdlibEmailMessage) -> None:
        self.threads.append(threading.get_ident())
        if self.error is not None:
            raise self.error
        self.sent.append(message)


def _install_sender(
    monkeypatch: pytest.MonkeyPatch,
    sender: _RecordingSender,
    settings: Settings | None = None,
) -> None:
    """Point ``_deliver`` at ``sender`` without touching the network."""
    resolved = settings or _settings()
    monkeypatch.setattr(email_mod, "get_settings", lambda: resolved)
    monkeypatch.setattr(email_mod, "_get_sender", lambda *_a, **_kw: sender)


# --------------------------------------------------------------------
# 1. _classify: exception -> discriminated code, and retriability
# --------------------------------------------------------------------

_CLASSIFY_CASES: list[tuple[str, BaseException, str, bool]] = [
    (
        "auth",
        smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication credentials invalid"),
        ERROR_AUTH,
        False,
    ),
    (
        "recipients_refused",
        smtplib.SMTPRecipientsRefused({"consultant@example.test": (550, b"No such user")}),
        ERROR_RECIPIENT_REFUSED,
        False,
    ),
    (
        "sender_refused",
        smtplib.SMTPSenderRefused(550, b"Sender address rejected", "no-reply@bitvision.test"),
        ERROR_SENDER_REFUSED,
        False,
    ),
    (
        "server_disconnected",
        smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
        ERROR_DISCONNECTED,
        True,
    ),
    ("tls", ssl.SSLError("WRONG_VERSION_NUMBER"), ERROR_TLS, False),
    ("dns", socket.gaierror(-2, "Name or service not known"), ERROR_DNS, True),
    ("timeout", TimeoutError("timed out"), ERROR_CONNECT_TIMEOUT, True),
    (
        "enetunreach",
        OSError(errno.ENETUNREACH, "Network is unreachable"),
        ERROR_UNREACHABLE,
        True,
    ),
    (
        "ehostunreach",
        OSError(errno.EHOSTUNREACH, "No route to host"),
        ERROR_UNREACHABLE,
        True,
    ),
    (
        "econnrefused",
        OSError(errno.ECONNREFUSED, "Connection refused"),
        ERROR_CONNECT_FAILED,
        True,
    ),
    ("generic_oserror", OSError("broken pipe"), ERROR_CONNECT_FAILED, True),
    ("non_oserror", ValueError("something else entirely"), ERROR_UNKNOWN, True),
]


@pytest.mark.parametrize(
    ("exc", "expected_code", "expected_retriable"),
    [(exc, code, retriable) for _id, exc, code, retriable in _CLASSIFY_CASES],
    ids=[case[0] for case in _CLASSIFY_CASES],
)
def test_classify_maps_exception_to_code(
    exc: BaseException, expected_code: str, expected_retriable: bool
) -> None:
    err = _classify(exc, HOST, PORT)
    assert isinstance(err, EmailDeliveryError)
    assert err.error_code == expected_code
    assert err.retriable is expected_retriable


@pytest.mark.parametrize(
    "exc",
    [exc for _id, exc, _code, _retriable in _CLASSIFY_CASES],
    ids=[case[0] for case in _CLASSIFY_CASES],
)
def test_classify_detail_names_the_endpoint(exc: BaseException) -> None:
    """Operators need to know *which* relay refused them."""
    err = _classify(exc, HOST, PORT)
    assert HOST in err.detail
    # DNS failures name the host only: there is no port to speak of
    # when the name never resolved.
    if err.error_code != ERROR_DNS:
        assert str(PORT) in err.detail


def test_classify_is_ordered_smtplib_before_oserror() -> None:
    """``smtplib.SMTPException`` subclasses ``OSError``.

    If the OSError branches were checked first, every protocol-level
    refusal would collapse into ``smtp_connect_failed`` and the ledger
    would retry a permanently-refused recipient forever.
    """
    assert issubclass(smtplib.SMTPException, OSError)
    assert issubclass(ssl.SSLError, OSError)
    assert issubclass(socket.gaierror, OSError)
    assert issubclass(TimeoutError, OSError)
    for exc, expected in (
        (smtplib.SMTPServerDisconnected("bye"), ERROR_DISCONNECTED),
        (ssl.SSLError("handshake"), ERROR_TLS),
        (socket.gaierror(-2, "nope"), ERROR_DNS),
        (TimeoutError("slow"), ERROR_CONNECT_TIMEOUT),
    ):
        assert _classify(exc, HOST, PORT).error_code == expected


def test_classify_passes_through_existing_delivery_error() -> None:
    """A typed error raised deeper down must not be re-wrapped."""
    original = EmailDeliveryError(ERROR_INSECURE_AUTH, "already classified")
    assert _classify(original, HOST, PORT) is original


def test_retriable_partition_is_exhaustive() -> None:
    """Every ``ERROR_*`` constant is explicitly on one side of the split.

    Adding a code without deciding its retriability would silently
    inherit ``False`` (never retried) and quietly drop mail.
    """
    retriable = {
        ERROR_CONNECT_TIMEOUT,
        ERROR_UNREACHABLE,
        ERROR_CONNECT_FAILED,
        ERROR_DNS,
        ERROR_DISCONNECTED,
        ERROR_UNKNOWN,
    }
    non_retriable = {
        ERROR_AUTH,
        ERROR_RECIPIENT_REFUSED,
        ERROR_SENDER_REFUSED,
        ERROR_TLS,
        ERROR_INSECURE_AUTH,
        ERROR_NOT_CONFIGURED,
    }
    declared = {getattr(email_mod, name) for name in dir(email_mod) if name.startswith("ERROR_")}
    assert declared == retriable | non_retriable
    for code in retriable:
        assert EmailDeliveryError(code, "x").retriable is True, code
    for code in non_retriable:
        assert EmailDeliveryError(code, "x").retriable is False, code


def test_delivery_outcome_round_trip() -> None:
    assert DeliveryOutcome.success() == DeliveryOutcome(ok=True)
    exc = EmailDeliveryError(ERROR_AUTH, "relay rejected the credentials")
    outcome = DeliveryOutcome.from_error(exc)
    assert outcome.ok is False
    assert outcome.error_code == ERROR_AUTH
    assert outcome.error_detail == "relay rejected the credentials"
    assert outcome.retriable is False


# --------------------------------------------------------------------
# 2. _connect_failure precedence — the 2026-07-31 regression
# --------------------------------------------------------------------


@pytest.fixture
def _stub_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``_describe_targets`` off the resolver."""
    monkeypatch.setattr(
        email_mod, "_describe_targets", lambda host, port: "IPv4 10.0.0.1, IPv6 2001:db8::1"
    )


def test_connect_failure_timeout_outranks_unreachable(_stub_targets: None) -> None:
    """THE regression.

    A dual-stack relay reached from a v4-only pod produces an IPv4
    ``TimeoutError`` (the blocked submission port — the real cause) and
    a trailing IPv6 ``ENETUNREACH`` (expected, meaningless). Reporting
    the trailing one is what sent the outage diagnosis after an
    imaginary IPv6 routing problem for hours.
    """
    err = _connect_failure(
        HOST,
        PORT,
        [TimeoutError("timed out"), OSError(errno.ENETUNREACH, "Network is unreachable")],
    )
    assert err.error_code == ERROR_CONNECT_TIMEOUT
    assert err.error_code != ERROR_UNREACHABLE
    assert err.retriable is True
    assert HOST in err.detail
    assert str(PORT) in err.detail


def test_connect_failure_timeout_outranks_unreachable_in_any_order(_stub_targets: None) -> None:
    """Ordering of the attempt list must not change the verdict."""
    err = _connect_failure(
        HOST,
        PORT,
        [OSError(errno.ENETUNREACH, "Network is unreachable"), TimeoutError("timed out")],
    )
    assert err.error_code == ERROR_CONNECT_TIMEOUT


def test_connect_failure_all_unreachable_reports_unreachable(_stub_targets: None) -> None:
    err = _connect_failure(
        HOST,
        PORT,
        [
            OSError(errno.ENETUNREACH, "Network is unreachable"),
            OSError(errno.EHOSTUNREACH, "No route to host"),
        ],
    )
    assert err.error_code == ERROR_UNREACHABLE
    assert err.retriable is True


def test_connect_failure_mixed_errno_falls_back_to_connect_failed(_stub_targets: None) -> None:
    """One non-unreachable errno in the set demotes the whole verdict."""
    err = _connect_failure(
        HOST,
        PORT,
        [
            OSError(errno.ENETUNREACH, "Network is unreachable"),
            OSError(errno.ECONNREFUSED, "Connection refused"),
        ],
    )
    assert err.error_code == ERROR_CONNECT_FAILED


def test_connect_failure_empty_list_is_connect_failed(_stub_targets: None) -> None:
    err = _connect_failure(HOST, PORT, [])
    assert err.error_code == ERROR_CONNECT_FAILED
    assert "no detail" in err.detail


def test_connect_failure_detail_lists_every_attempt(_stub_targets: None) -> None:
    """All attempts survive into the detail, not just the last one."""
    err = _connect_failure(
        HOST,
        PORT,
        [TimeoutError("timed out"), OSError(errno.ENETUNREACH, "Network is unreachable")],
    )
    assert "TimeoutError" in err.detail
    assert "Network is unreachable" in err.detail
    assert "IPv4 10.0.0.1" in err.detail
    assert "IPv6 2001:db8::1" in err.detail


def test_describe_targets_never_masks_the_delivery_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostics are best-effort: a broken resolver must not raise."""

    def _boom(*_a: object, **_kw: object) -> object:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    assert _describe_targets(HOST, PORT) == "unresolved"
    err = _connect_failure(HOST, PORT, [TimeoutError("timed out")])
    assert err.error_code == ERROR_CONNECT_TIMEOUT
    assert "unresolved" in err.detail


def test_diagnostic_connect_unwraps_exception_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_connection`` must be asked for *all* errors, not the last.

    Without ``all_errors=True`` the stdlib discards every attempt but
    the trailing one, which is precisely how the IPv4 timeout got lost
    behind the IPv6 unreachable.
    """
    seen: dict[str, Any] = {}

    def _fake_create_connection(
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: object = None,
        *,
        all_errors: bool = False,
    ) -> object:
        seen["address"] = address
        seen["timeout"] = timeout
        seen["all_errors"] = all_errors
        raise ExceptionGroup(
            "create_connection failed",
            [TimeoutError("timed out"), OSError(errno.ENETUNREACH, "Network is unreachable")],
        )

    monkeypatch.setattr(socket, "create_connection", _fake_create_connection)
    monkeypatch.setattr(email_mod, "_describe_targets", lambda host, port: "IPv4 10.0.0.1")

    with pytest.raises(EmailDeliveryError) as excinfo:
        _diagnostic_connect(HOST, PORT, 7.0, None)

    assert seen["all_errors"] is True
    assert seen["address"] == (HOST, PORT)
    assert seen["timeout"] == 7.0
    assert excinfo.value.error_code == ERROR_CONNECT_TIMEOUT
    assert excinfo.value.retriable is True


# --------------------------------------------------------------------
# 3. send_email_sync reports instead of raising
# --------------------------------------------------------------------


def test_send_email_sync_success_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _RecordingSender()
    _install_sender(monkeypatch, sender)
    outcome = send_email_sync(_message())
    assert outcome == DeliveryOutcome(ok=True)
    assert outcome.error_code is None
    assert len(sender.sent) == 1
    assert sender.sent[0]["To"] == "consultant@example.test"


def test_send_email_sync_retriable_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sender(
        monkeypatch,
        _RecordingSender(EmailDeliveryError(ERROR_CONNECT_TIMEOUT, f"{HOST}:{PORT} timed out")),
    )
    outcome = send_email_sync(_message())
    assert outcome.ok is False
    assert outcome.error_code == ERROR_CONNECT_TIMEOUT
    assert outcome.retriable is True
    assert outcome.error_detail is not None
    assert HOST in outcome.error_detail


def test_send_email_sync_non_retriable_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sender(
        monkeypatch,
        _RecordingSender(EmailDeliveryError(ERROR_AUTH, f"{HOST}:{PORT} rejected the credentials")),
    )
    outcome = send_email_sync(_message())
    assert outcome.ok is False
    assert outcome.error_code == ERROR_AUTH
    assert outcome.retriable is False


def test_send_email_sync_normalises_untyped_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw socket error still comes back as a discriminated code."""
    _install_sender(monkeypatch, _RecordingSender(socket.gaierror(-2, "Name or service not known")))
    outcome = send_email_sync(_message())
    assert outcome.ok is False
    assert outcome.error_code == ERROR_DNS
    assert outcome.retriable is True


def test_send_email_sync_logs_the_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install_sender(
        monkeypatch, _RecordingSender(EmailDeliveryError(ERROR_AUTH, "credentials rejected"))
    )
    with caplog.at_level("ERROR", logger="bvphoenix.services.email"):
        send_email_sync(_message())
    assert any(ERROR_AUTH in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------
# 4. send_email (async) raises — the core regression
# --------------------------------------------------------------------


async def test_send_email_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old signature was ``-> None`` and swallowed everything.

    That is what let the notify endpoint answer 200 for undelivered
    mail. ``send_email`` must now propagate.
    """
    _install_sender(
        monkeypatch,
        _RecordingSender(EmailDeliveryError(ERROR_CONNECT_TIMEOUT, f"{HOST}:{PORT} timed out")),
    )
    with pytest.raises(EmailDeliveryError) as excinfo:
        await send_email(_message())
    assert excinfo.value.error_code == ERROR_CONNECT_TIMEOUT
    assert excinfo.value.retriable is True
    assert HOST in excinfo.value.detail


async def test_send_email_raises_on_non_retriable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sender(
        monkeypatch, _RecordingSender(EmailDeliveryError(ERROR_AUTH, "credentials rejected"))
    )
    with pytest.raises(EmailDeliveryError) as excinfo:
        await send_email(_message())
    assert excinfo.value.error_code == ERROR_AUTH
    assert excinfo.value.retriable is False


async def test_send_email_normalises_untyped_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_sender(monkeypatch, _RecordingSender(ssl.SSLError("WRONG_VERSION_NUMBER")))
    with pytest.raises(EmailDeliveryError) as excinfo:
        await send_email(_message())
    assert excinfo.value.error_code == ERROR_TLS


async def test_send_email_success_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _RecordingSender()
    _install_sender(monkeypatch, sender)
    assert await send_email(_message()) is None
    assert len(sender.sent) == 1


async def test_send_email_offloads_the_blocking_socket_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocking connect must not run on the event loop.

    The backend image starts uvicorn without ``--workers``; a inline
    connect froze ``/health`` for the whole timeout during the outage.
    """
    sender = _RecordingSender()
    _install_sender(monkeypatch, sender)
    await send_email(_message())
    assert sender.threads and sender.threads[0] != threading.get_ident()


# --------------------------------------------------------------------
# 5. Never authenticate in the clear
# --------------------------------------------------------------------


def test_smtp_sender_refuses_login_over_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential + ``smtp_security='none'`` must abort before AUTH.

    The credential is a relay API token and the body carries a landing
    URL that grants access to PHI; neither may cross the wire in clear.
    """
    transcript = _install_fake_smtp(monkeypatch)
    settings = _settings(smtp_security="none", smtp_username="api-token", smtp_password="s3cret")
    sender = SmtpEmailSender(settings)

    with pytest.raises(EmailDeliveryError) as excinfo:
        sender.send(email_mod._to_stdlib(_message(), settings))

    assert excinfo.value.error_code == ERROR_INSECURE_AUTH
    assert excinfo.value.retriable is False
    assert "login" not in transcript.calls
    assert transcript.logins == []
    assert "send_message" not in transcript.calls
    assert transcript.sent == []
    # The connection is still torn down by the context manager.
    assert transcript.entered == 1
    assert transcript.exited == 1


def test_smtp_sender_refusal_names_the_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_smtp(monkeypatch)
    settings = _settings(smtp_security="none", smtp_username="api-token")
    with pytest.raises(EmailDeliveryError) as excinfo:
        SmtpEmailSender(settings).send(email_mod._to_stdlib(_message(), settings))
    assert HOST in excinfo.value.detail
    assert str(PORT) in excinfo.value.detail
    assert "smtp_security='none'" in excinfo.value.detail


def test_smtp_sender_plaintext_without_credentials_still_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is about the credential, not about plaintext itself."""
    transcript = _install_fake_smtp(monkeypatch)
    settings = _settings(smtp_security="none", smtp_username="")
    SmtpEmailSender(settings).send(email_mod._to_stdlib(_message(), settings))
    assert "login" not in transcript.calls
    assert transcript.calls.count("send_message") == 1
    assert "starttls" not in transcript.calls


def test_smtp_sender_starttls_reissues_ehlo_before_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RFC 3207: the session resets on upgrade, so AUTH needs a new EHLO."""
    transcript = _install_fake_smtp(monkeypatch)
    settings = _settings(smtp_security="starttls", smtp_username="api-token", smtp_password="pw")
    SmtpEmailSender(settings).send(email_mod._to_stdlib(_message(), settings))
    assert transcript.calls == ["ehlo", "starttls", "ehlo", "login", "send_message"]
    assert transcript.logins == [("api-token", "pw")]
    assert transcript.kind == "plain"
    assert transcript.timeout == 7


def test_smtp_sender_implicit_tls_uses_the_ssl_client(monkeypatch: pytest.MonkeyPatch) -> None:
    transcript = _install_fake_smtp(monkeypatch)
    settings = _settings(smtp_security="implicit", smtp_port=2465, smtp_username="api-token")
    SmtpEmailSender(settings).send(email_mod._to_stdlib(_message(), settings))
    assert transcript.kind == "implicit"
    assert transcript.port == 2465
    assert isinstance(transcript.tls_context, ssl.SSLContext)
    # No STARTTLS upgrade: the channel is already encrypted.
    assert transcript.calls == ["ehlo", "login", "send_message"]


def test_smtp_sender_classifies_relay_rejections(monkeypatch: pytest.MonkeyPatch) -> None:
    """A protocol-level refusal surfaces as a typed error, not a raw one."""
    _install_fake_smtp(
        monkeypatch,
        raise_on_send=smtplib.SMTPSenderRefused(
            550, b"Sender address rejected", "no-reply@bitvision.test"
        ),
    )
    settings = _settings()
    with pytest.raises(EmailDeliveryError) as excinfo:
        SmtpEmailSender(settings).send(email_mod._to_stdlib(_message(), settings))
    assert excinfo.value.error_code == ERROR_SENDER_REFUSED
    assert excinfo.value.retriable is False


def test_smtp_sender_classifies_auth_rejections(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_smtp(
        monkeypatch,
        raise_on_login=smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials"),
    )
    settings = _settings(smtp_username="api-token", smtp_password="wrong")
    with pytest.raises(EmailDeliveryError) as excinfo:
        SmtpEmailSender(settings).send(email_mod._to_stdlib(_message(), settings))
    assert excinfo.value.error_code == ERROR_AUTH
    assert excinfo.value.retriable is False


def test_smtp_sender_reads_transport_settings() -> None:
    sender = SmtpEmailSender(
        _settings(smtp_security="", smtp_use_tls=False, smtp_timeout_seconds=3)
    )
    assert sender.host == HOST
    assert sender.port == PORT
    assert sender.security == "none"
    assert sender.timeout == 3


# --------------------------------------------------------------------
# 6. _get_sender precedence
# --------------------------------------------------------------------


def test_get_sender_default_provider_with_host_is_smtp() -> None:
    """Regression guard for the old ``stub`` default.

    With ``stub`` as the default, a single missing ConfigMap key routed
    100% of production mail into a file inside an ephemeral pod while
    every layer above reported success.
    """
    sender = _get_sender(_settings(email_provider="", smtp_host=HOST))
    assert isinstance(sender, SmtpEmailSender)
    assert not isinstance(sender, DevEmailSender)
    assert sender.host == HOST


def test_get_sender_explicit_smtp_provider_with_host_is_smtp() -> None:
    sender = _get_sender(_settings(email_provider="smtp", smtp_host=HOST))
    assert isinstance(sender, SmtpEmailSender)


@pytest.mark.parametrize("provider", ["stub", "log"])
def test_get_sender_explicit_stub_wins_over_host(provider: str) -> None:
    """``stub`` / ``log`` are an explicit opt-out, honoured with a host set."""
    sender = _get_sender(_settings(email_provider=provider, smtp_host=HOST))
    assert isinstance(sender, DevEmailSender)


def test_get_sender_smtp_provider_without_host_raises() -> None:
    with pytest.raises(EmailDeliveryError) as excinfo:
        _get_sender(_settings(email_provider="smtp", smtp_host=""))
    assert excinfo.value.error_code == ERROR_NOT_CONFIGURED
    assert excinfo.value.retriable is False


def test_get_sender_no_provider_no_host_is_dev() -> None:
    assert isinstance(_get_sender(_settings(email_provider="", smtp_host="")), DevEmailSender)


def test_get_sender_defaults_to_get_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Called with no argument it must read live settings, not a stub."""
    monkeypatch.setattr(email_mod, "get_settings", lambda: _settings(smtp_host=HOST))
    assert isinstance(_get_sender(), SmtpEmailSender)


def test_deliver_surfaces_not_configured_as_a_delivery_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured relay is reported, never written to a file."""
    monkeypatch.setattr(
        email_mod, "get_settings", lambda: _settings(email_provider="smtp", smtp_host="")
    )
    outcome = send_email_sync(_message())
    assert outcome.ok is False
    assert outcome.error_code == ERROR_NOT_CONFIGURED
    assert outcome.retriable is False


# --------------------------------------------------------------------
# 7. resolved_smtp_security
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("security", "use_tls", "expected"),
    [
        ("starttls", False, "starttls"),
        ("starttls", True, "starttls"),
        ("implicit", False, "implicit"),
        ("implicit", True, "implicit"),
        ("none", True, "none"),
        ("none", False, "none"),
    ],
)
def test_resolved_smtp_security_explicit_wins(security: str, use_tls: bool, expected: str) -> None:
    """``smtp_security`` overrides the deprecated boolean in both directions."""
    settings = _settings(smtp_security=security, smtp_use_tls=use_tls)
    assert settings.resolved_smtp_security == expected


@pytest.mark.parametrize(("use_tls", "expected"), [(True, "starttls"), (False, "none")])
def test_resolved_smtp_security_falls_back_to_use_tls(use_tls: bool, expected: str) -> None:
    """An existing ``BVP_SMTP_USE_TLS`` deployment keeps its behaviour."""
    settings = _settings(smtp_security="", smtp_use_tls=use_tls)
    assert settings.resolved_smtp_security == expected
