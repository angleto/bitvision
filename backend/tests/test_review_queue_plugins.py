"""Common plugin tests — content-level, no Postgres, no external clamd.

ClamAV is exercised against an in-process asyncio server speaking the
real clamd INSTREAM wire protocol (length-prefixed chunks, ``z``
framing), so ``services/clamav.py`` is covered end-to-end including
the EICAR-positive path; the EICAR literal is assembled from two
halves so repo scanners don't flag this file itself.
"""

from __future__ import annotations

import asyncio
import io
import struct
import uuid
import zipfile

import pytest
import pytest_asyncio

from bvphoenix.services.archive_guards import scan_zip_safety
from bvphoenix.services.clamav import ClamdClient
from bvphoenix.services.review_queue.checks import CheckContext, StagedComponent, StagedItem
from bvphoenix.services.review_queue.jobs import enqueue_review_checks, review_checks_job_id
from bvphoenix.services.review_queue.plugins import (
    ArchiveGuardCheck,
    ClamAVCheck,
    DedupCheck,
    DicomRouteCheck,
    MagicAllowlistCheck,
)

# Assembled at runtime (not a contiguous literal) so AV scans of the
# repository itself never flag this source file.
_EICAR_HEAD = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$"
_EICAR_TAIL = b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
EICAR = _EICAR_HEAD + _EICAR_TAIL

PDF = b"%PDF-1.4 minimal"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
DICOM = b"\x00" * 128 + b"DICM" + b"\x02\x00\x00\x00UL\x04\x00"


def component(name: str, payload: bytes, content_type: str | None = None) -> StagedComponent:
    async def _read() -> bytes:
        return payload

    return StagedComponent(
        name=name, size_bytes=len(payload), content_type=content_type, read=_read
    )


def ctx_for(*components: StagedComponent) -> CheckContext:
    staged = StagedItem(item_id=uuid.uuid4(), components=list(components))
    return CheckContext(db=None, staged=staged)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fake clamd
# ---------------------------------------------------------------------------


async def _serve_clamd(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    command = await reader.readuntil(b"\x00")
    if command == b"zPING\x00":
        writer.write(b"PONG\x00")
        await writer.drain()
        writer.close()
        return
    if command == b"zINSTREAM\x00":
        payload = bytearray()
        while True:
            (size,) = struct.unpack(">I", await reader.readexactly(4))
            if size == 0:
                break
            payload.extend(await reader.readexactly(size))
        if EICAR in payload:
            writer.write(b"stream: Win.Test.EICAR_HDB-1 FOUND\x00")
        else:
            writer.write(b"stream: OK\x00")
        await writer.drain()
    writer.close()


@pytest_asyncio.fixture
async def fake_clamd() -> ClamdClient:
    server = await asyncio.start_server(_serve_clamd, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield ClamdClient(host="127.0.0.1", port=port, timeout_s=5.0)
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# ClamAV
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clamd_client_ping(fake_clamd: ClamdClient) -> None:
    assert await fake_clamd.ping() is True


@pytest.mark.asyncio
async def test_clamav_clean_passes(fake_clamd: ClamdClient) -> None:
    result = await ClamAVCheck(fake_clamd).run(ctx_for(component("ok.pdf", PDF)))
    assert result.verdict == "pass"
    assert result.details["components"]["ok.pdf"]["status"] == "clean"


@pytest.mark.asyncio
async def test_clamav_eicar_blocks(fake_clamd: ClamdClient) -> None:
    result = await ClamAVCheck(fake_clamd).run(
        ctx_for(component("ok.pdf", PDF), component("evil.bin", EICAR))
    )
    assert result.verdict == "block"
    entry = result.details["components"]["evil.bin"]
    assert entry["status"] == "infected"
    assert "EICAR" in entry["signature"]


@pytest.mark.asyncio
async def test_clamav_large_payload_chunked(fake_clamd: ClamdClient) -> None:
    # >1 MiB exercises the multi-chunk INSTREAM path
    big = PDF + b"\x00" * (3 * 1024 * 1024) + EICAR
    result = await ClamAVCheck(fake_clamd).run(ctx_for(component("big.bin", big)))
    assert result.verdict == "block"


@pytest.mark.asyncio
async def test_clamav_unreachable_is_error_never_clean() -> None:
    dead = ClamdClient(host="127.0.0.1", port=1, timeout_s=0.2)
    result = await ClamAVCheck(dead).run(ctx_for(component("a.pdf", PDF)))
    assert result.verdict == "error"
    assert await dead.ping() is False


# ---------------------------------------------------------------------------
# Magic allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"MZ\x90\x00" + b"\x00" * 64, "pe-executable"),
        (b"\x7fELF" + b"\x00" * 64, "elf-executable"),
        (b"\xcf\xfa\xed\xfe" + b"\x00" * 64, "mach-o"),
        (b"#!/bin/sh\nrm -rf /", "script-shebang"),
    ],
)
async def test_allowlist_blocks_executables(payload: bytes, reason: str) -> None:
    result = await MagicAllowlistCheck().run(ctx_for(component("f.bin", payload)))
    assert result.verdict == "block"
    assert result.details["components"]["f.bin"]["reason"] == reason


@pytest.mark.asyncio
async def test_allowlist_accepts_clinical_types() -> None:
    result = await MagicAllowlistCheck().run(
        ctx_for(
            component("r.pdf", PDF),
            component("scan.png", PNG),
            component("img.dcm", DICOM),
        )
    )
    assert result.verdict == "pass"
    assert result.details["components"]["img.dcm"]["kind"] == "dicom"


@pytest.mark.asyncio
async def test_allowlist_fails_unknown_and_disallowed_kinds() -> None:
    junk = await MagicAllowlistCheck().run(ctx_for(component("x", b"\x01\x02\x03\x04")))
    assert junk.verdict == "fail"
    assert junk.details["components"]["x"]["reason"] == "unclassifiable"

    wav = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 8
    audio = await MagicAllowlistCheck().run(ctx_for(component("dictation.wav", wav)))
    assert audio.verdict == "fail"  # audio outside the default allowlist


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_hit_warns_with_matches() -> None:
    import hashlib

    known = hashlib.sha256(PDF).hexdigest()
    seen: list[str] = []

    async def lookup(db, digest: str):
        seen.append(digest)
        return ["doc-1", "doc-2"] if digest == known else []

    result = await DedupCheck(lookup).run(
        ctx_for(component("dup.pdf", PDF), component("new.png", PNG))
    )
    assert result.verdict == "warn"
    assert result.details["components"]["dup.pdf"]["duplicate_of"] == ["doc-1", "doc-2"]
    assert "duplicate_of" not in result.details["components"]["new.png"]
    assert seen == [known, hashlib.sha256(PNG).hexdigest()]


# ---------------------------------------------------------------------------
# DICOM route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dicom_route_signal() -> None:
    result = await DicomRouteCheck().run(
        ctx_for(component("img.dcm", DICOM), component("r.pdf", PDF))
    )
    assert result.verdict == "pass"  # routing signal, never a gate
    assert result.details["components"] == {"img.dcm": "dicom", "r.pdf": "other"}
    assert result.details["route"] == "dicom"

    docs_only = await DicomRouteCheck().run(ctx_for(component("r.pdf", PDF)))
    assert docs_only.details["route"] == "document"


# ---------------------------------------------------------------------------
# Archive guard
# ---------------------------------------------------------------------------


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_archive_guard_accepts_sane_zip() -> None:
    blob = _zip_bytes({"study/img1.dcm": DICOM, "study/referto.pdf": PDF})
    result = await ArchiveGuardCheck().run(ctx_for(component("study.zip", blob)))
    assert result.verdict == "pass"


@pytest.mark.asyncio
async def test_archive_guard_blocks_traversal_member() -> None:
    blob = _zip_bytes({"../../etc/passwd": b"root:x:0:0"})
    result = await ArchiveGuardCheck().run(ctx_for(component("evil.zip", blob)))
    assert result.verdict == "block"
    assert any(
        "unsafe member path" in h for h in result.details["components"]["evil.zip"]["hostile"]
    )


@pytest.mark.asyncio
async def test_archive_guard_blocks_zip_bomb() -> None:
    # 600 MB of zeros declared, compresses to ~KBs: ratio >> 200
    blob = _zip_bytes({"zeros.bin": b"\x00" * (600 * 1024 * 1024)})
    result = await ArchiveGuardCheck().run(ctx_for(component("bomb.zip", blob)))
    assert result.verdict == "block"


@pytest.mark.asyncio
async def test_archive_guard_flags_encrypted_member() -> None:
    # ``zipfile`` recomputes flag bits on write, so set the encryption
    # bit (bit 0 of the general-purpose flags) directly in the central
    # directory header (PK\x01\x02, flags at offset +8) — that is what
    # ``infolist`` reads back.
    raw = bytearray(_zip_bytes({"locked.pdf": PDF}))
    cd = raw.find(b"PK\x01\x02")
    assert cd != -1
    raw[cd + 8] |= 0x1
    result = await ArchiveGuardCheck().run(ctx_for(component("z.zip", bytes(raw))))
    assert result.verdict == "fail"
    assert any("encrypted member" in s for s in result.details["components"]["z.zip"]["suspicious"])


@pytest.mark.asyncio
async def test_archive_guard_blocks_oversized_component_without_reading() -> None:
    async def explode() -> bytes:
        raise AssertionError("oversized component must not be read")

    comp = StagedComponent(name="huge.bin", size_bytes=10**12, content_type=None, read=explode)
    result = await ArchiveGuardCheck().run(ctx_for(comp))
    assert result.verdict == "block"


def test_scan_zip_safety_handles_garbage() -> None:
    report = scan_zip_safety(b"not a zip at all")
    assert report.is_hostile


# ---------------------------------------------------------------------------
# Job glue
# ---------------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.known: set[str] = set()

    async def enqueue_job(self, name, *args, _job_id=None):
        self.calls.append((name, args, _job_id))
        if _job_id in self.known:
            return None  # arq dedup behaviour
        self.known.add(_job_id)
        return object()


@pytest.mark.asyncio
async def test_enqueue_review_checks_dedups_by_etag() -> None:
    redis = FakeRedis()
    item_id, etag = uuid.uuid4(), uuid.uuid4()

    first = await enqueue_review_checks(redis, profile_name="p", item_id=item_id, etag=etag)
    dup = await enqueue_review_checks(redis, profile_name="p", item_id=item_id, etag=etag)
    assert first is not None
    assert dup is None  # same staged state: collapsed

    moved = await enqueue_review_checks(redis, profile_name="p", item_id=item_id, etag=uuid.uuid4())
    assert moved is not None  # state moved (etag bumped): new run allowed

    name, args, job_id = redis.calls[0]
    assert name == "run_review_checks"
    assert args == ("p", str(item_id))
    assert job_id == review_checks_job_id("p", item_id, etag)


@pytest.mark.asyncio
async def test_requeue_stale_requires_updated_at() -> None:
    from bvphoenix.services.review_queue.jobs import requeue_stale_processing

    class NoTimestamps:
        pass

    with pytest.raises(TypeError, match="updated_at"):
        await requeue_stale_processing(None, FakeRedis(), model=NoTimestamps, profile_name="p")
