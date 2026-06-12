"""Minimal async clamd client (INSTREAM over TCP).

Talks to the in-cluster ``clamav`` Service (see
``deploy/bvphoenix-production-k8s-deploy/clamav-deployment.yaml``).
Only the two commands the review queue needs are implemented — PING
for readiness probes and INSTREAM for scanning staged blobs — using the
null-terminated ``z`` command framing from ``clamd(8)``:

* request: ``zINSTREAM\\0`` then length-prefixed chunks (4-byte
  big-endian size + payload), terminated by a zero-length chunk;
* response: ``stream: OK\\0`` or ``stream: <Signature> FOUND\\0``.

The server-side ``StreamMaxLength`` must cover the largest staged
component (the ConfigMap sets 600M, aligned with
``archive_guards.MAX_FILE_BYTES``); clamd answers ``INSTREAM size
limit exceeded`` past it, which surfaces here as ``error``.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from bvphoenix.config import get_settings

_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ScanResult:
    status: str  # "clean" | "infected" | "error"
    signature: str | None = None
    detail: str | None = None

    @property
    def is_clean(self) -> bool:
        return self.status == "clean"

    @property
    def is_infected(self) -> bool:
        return self.status == "infected"


class ClamdClient:
    def __init__(self, host: str, port: int = 3310, timeout_s: float = 60.0) -> None:
        if not host:
            raise ValueError("clamd host is not configured (BVP_CLAMD_HOST)")
        self._host = host
        self._port = port
        self._timeout_s = timeout_s

    @classmethod
    def from_settings(cls) -> ClamdClient:
        s = get_settings()
        return cls(host=s.clamd_host, port=s.clamd_port, timeout_s=s.clamd_timeout_s)

    async def _roundtrip(self, payload_writer) -> bytes:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), timeout=self._timeout_s
        )
        try:
            await payload_writer(writer)
            await asyncio.wait_for(writer.drain(), timeout=self._timeout_s)
            return await asyncio.wait_for(reader.readuntil(b"\x00"), timeout=self._timeout_s)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def ping(self) -> bool:
        try:

            async def _send(writer: asyncio.StreamWriter) -> None:
                writer.write(b"zPING\x00")

            resp = await self._roundtrip(_send)
        except (TimeoutError, OSError, asyncio.IncompleteReadError):
            return False
        return resp.rstrip(b"\x00").strip() == b"PONG"

    async def scan_bytes(self, data: bytes) -> ScanResult:
        """Scan one in-memory blob. Network/daemon trouble is reported as
        ``error`` — never as ``clean`` — so callers cannot mistake an
        unscanned payload for a safe one."""
        try:

            async def _send(writer: asyncio.StreamWriter) -> None:
                writer.write(b"zINSTREAM\x00")
                for off in range(0, len(data), _CHUNK_BYTES):
                    chunk = data[off : off + _CHUNK_BYTES]
                    writer.write(struct.pack(">I", len(chunk)))
                    writer.write(chunk)
                    # Yield to the loop between chunks so a multi-hundred-MB
                    # payload doesn't monopolise it; drain applies backpressure.
                    await writer.drain()
                writer.write(struct.pack(">I", 0))

            raw = await self._roundtrip(_send)
        except (TimeoutError, OSError, asyncio.IncompleteReadError) as exc:
            return ScanResult(status="error", detail=f"clamd unreachable: {exc}")

        text = raw.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
        if text.endswith(" OK"):
            return ScanResult(status="clean")
        if text.endswith(" FOUND"):
            # "stream: Win.Test.EICAR_HDB-1 FOUND"
            middle = text[: -len(" FOUND")]
            signature = middle.split(":", 1)[-1].strip()
            return ScanResult(status="infected", signature=signature)
        return ScanResult(status="error", detail=f"unexpected clamd reply: {text!r}")


__all__ = ["ClamdClient", "ScanResult"]
