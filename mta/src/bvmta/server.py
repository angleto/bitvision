"""The SMTP handler: RCPT validation + raw forwarding, nothing else.

SMTP status mapping (the contract in ``api/internal_inbound_email.py``):

=================  =======================================
backend response   SMTP reply to the sender
=================  =======================================
200 validate-rcpt  250 (recipient accepted)
404 validate-rcpt  550 5.1.1 (unknown — also for revoked)
200/201 intake     250 (accepted; duplicates included)
413 intake         552 5.3.4 (message too large)
429 intake         451 4.7.1 (rate limited — retry later)
anything else      451 4.4.1 (temporary — retry later)
=================  =======================================

The 451 fallback is the load-bearing choice: a backend deploy, a
network blip or a misconfigured secret must surface to the sender as
"try again later", never as silent loss or a permanent bounce.

Anti-loop posture: the adapter never generates mail (no bounces, no
auto-replies — refusals happen inside the SMTP conversation), so
``Auto-Submitted`` loops cannot start here; the staging worker
additionally flags auto-submitted messages for the reviewer.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import sys

import httpx
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP, Envelope, Session

from bvmta.config import get_settings

logger = logging.getLogger(__name__)


class InboundHandler:
    """aiosmtpd handler delegating every decision to the backend."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        # Injectable for tests; lazily built so the controller can be
        # constructed before an event loop exists.
        self._client = client

    def _resolve_client(self) -> httpx.AsyncClient:
        if self._client is None:
            settings = get_settings()
            self._client = httpx.AsyncClient(
                base_url=settings.mta_backend_url,
                headers={"X-Inbound-Key": settings.inbound_internal_secret},
            )
        return self._client

    async def handle_RCPT(  # noqa: N802 (aiosmtpd contract)
        self,
        server: SMTP,
        session: Session,
        envelope: Envelope,
        address: str,
        rcpt_options: list[str],
    ) -> str:
        settings = get_settings()
        local_part = address.split("@", 1)[0] if "@" in address else address
        try:
            resp = await self._resolve_client().post(
                "/api/internal/inbound-email/validate-rcpt",
                json={"local_part": local_part},
                timeout=settings.mta_rcpt_timeout_s,
            )
        except httpx.HTTPError:
            logger.warning("validate-rcpt unreachable for %r", address, exc_info=True)
            return "451 4.4.1 Temporary failure, try again later"
        if resp.status_code == 200:
            envelope.rcpt_tos.append(address)
            return "250 OK"
        if resp.status_code == 404:
            return "550 5.1.1 User unknown"
        logger.warning("validate-rcpt for %r answered %s", address, resp.status_code)
        return "451 4.4.1 Temporary failure, try again later"

    async def handle_DATA(  # noqa: N802 (aiosmtpd contract)
        self, server: SMTP, session: Session, envelope: Envelope
    ) -> str:
        settings = get_settings()
        raw = envelope.original_content or envelope.content or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", errors="replace")
        client = self._resolve_client()
        # One backend delivery per accepted recipient: the same message
        # CC'd to two patients' addresses lands in both queues, deduped
        # per-address on the backend side.
        for rcpt in envelope.rcpt_tos:
            try:
                resp = await client.post(
                    "/api/internal/inbound-email",
                    content=raw,
                    headers={
                        "X-Envelope-Rcpt": rcpt,
                        "Content-Type": "message/rfc822",
                    },
                    timeout=settings.mta_data_timeout_s,
                )
            except httpx.HTTPError:
                logger.warning("inbound-email forward failed for %r", rcpt, exc_info=True)
                return "451 4.4.1 Temporary failure, try again later"
            if resp.status_code in (200, 201):
                continue
            if resp.status_code == 413:
                return "552 5.3.4 Message too large"
            if resp.status_code == 429:
                return "451 4.7.1 Rate limited, try again later"
            logger.warning("inbound-email for %r answered %s", rcpt, resp.status_code)
            return "451 4.4.1 Temporary failure, try again later"
        return "250 OK"


def build_controller(handler: InboundHandler | None = None) -> Controller:
    settings = get_settings()
    tls_context: ssl.SSLContext | None = None
    if settings.mta_tls_cert_file and settings.mta_tls_key_file:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.load_cert_chain(settings.mta_tls_cert_file, settings.mta_tls_key_file)
    return Controller(
        handler or InboundHandler(),
        hostname=settings.mta_bind_host,
        port=settings.mta_bind_port,
        server_hostname=settings.mta_hostname,
        data_size_limit=settings.inbound_email_max_raw_bytes,
        tls_context=tls_context,
        # Opportunistic: legacy hospital relays without TLS must still
        # deliver; the upgrade path (MTA-STS) is a future DNS change.
        require_starttls=False,
        decode_data=False,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    if not settings.inbound_internal_secret:
        # Fail closed, loudly: without the secret every forward would
        # bounce off the backend's 401 as an opaque 451 storm.
        logger.error("BVP_INBOUND_INTERNAL_SECRET is unset; refusing to start")
        sys.exit(2)
    controller = build_controller()
    controller.start()
    logger.info(
        "bvmta listening on %s:%s as %s (max %s bytes)",
        settings.mta_bind_host,
        settings.mta_bind_port,
        settings.mta_hostname,
        settings.inbound_email_max_raw_bytes,
    )
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
