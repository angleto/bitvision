"""Common auto-check plugins — shared by both review profiles.

Profile-specific checks (SPF/DKIM sender verification for the patient
inbox; PS3.15 de-identification, burned-in-PHI OCR, CSAM/NSFW screening
for public contributions) live with their consumers. What is here is
the content-safety floor every staged item passes through regardless of
posture:

* :class:`ClamAVCheck` — malware scan via the in-cluster clamd;
* :class:`MagicAllowlistCheck` — magic-byte type gate, hard-blocks
  executables/scripts;
* :class:`DedupCheck` — flags blobs already known to the consumer store
  (``original_blob_hash`` convention);
* :class:`DicomRouteCheck` — routing signal: which components are DICOM
  Part-10 (drives the ingest pipeline choice at promotion);
* :class:`ArchiveGuardCheck` — size caps + zip-slip / zip-bomb guards
  (shared ``services/archive_guards`` engine).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from bvphoenix.services.archive_guards import (
    MAX_FILE_BYTES,
    scan_zip_safety,
)
from bvphoenix.services.clamav import ClamdClient
from bvphoenix.services.dicom_ingest import has_dicm_preamble
from bvphoenix.services.file_classifier import FileKind, classify_file
from bvphoenix.services.review_queue.checks import (
    CheckContext,
    CheckResult,
    aggregate_verdicts,
)

# Executable / script signatures — never legitimate in a clinical
# staging flow, in any profile. PE (Windows), ELF (Linux), Mach-O
# thin + fat (macOS, both endiannesses), and a shebang line.
_EXECUTABLE_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "pe-executable"),
    (b"\x7fELF", "elf-executable"),
    (b"\xfe\xed\xfa\xce", "mach-o"),
    (b"\xfe\xed\xfa\xcf", "mach-o"),
    (b"\xce\xfa\xed\xfe", "mach-o"),
    (b"\xcf\xfa\xed\xfe", "mach-o"),
    (b"\xca\xfe\xba\xbe", "mach-o-fat"),
    (b"#!", "script-shebang"),
)

# Default content allowlist: the media families the clinical pipelines
# can actually ingest. Audio/video are excluded by default (no consumer
# promotes them today); a profile that wants dictations passes its own.
DEFAULT_ALLOWED_KINDS: frozenset[FileKind] = frozenset(
    {FileKind.DICOM, FileKind.PDF, FileKind.IMAGE, FileKind.TEXT, FileKind.ARCHIVE}
)


def _executable_signature(content: bytes) -> str | None:
    for magic, label in _EXECUTABLE_MAGICS:
        if content.startswith(magic):
            return label
    return None


class ClamAVCheck:
    """Scan every component through clamd; any hit hard-blocks the item.

    A scanner outage yields ``error`` (item still needs a human eye and
    the run can be repeated) — see the verdict semantics in ``checks``.
    """

    name = "clamav"

    def __init__(self, client: ClamdClient | None = None) -> None:
        # Lazy default so unit tests and profiles can inject a fake; the
        # settings lookup happens at first run, not at import.
        self._client = client

    def _resolve(self) -> ClamdClient:
        if self._client is None:
            self._client = ClamdClient.from_settings()
        return self._client

    async def run(self, ctx: CheckContext) -> CheckResult:
        client = self._resolve()
        components: dict[str, dict] = {}
        verdicts: list[str] = []
        for comp in ctx.staged.components:
            res = await client.scan_bytes(await comp.read())
            if res.is_infected:
                components[comp.name] = {"status": "infected", "signature": res.signature}
                verdicts.append("block")
            elif res.is_clean:
                components[comp.name] = {"status": "clean"}
                verdicts.append("pass")
            else:
                components[comp.name] = {"status": "error", "detail": res.detail}
                verdicts.append("error")
        return CheckResult(
            verdict=aggregate_verdicts(verdicts),
            details={"components": components},
        )


class MagicAllowlistCheck:
    """Magic-byte type gate.

    Executables/scripts are hard-blocked; anything that classifies
    outside the profile's allowlist (or not at all) fails — magic bytes
    outrank both the declared content-type and the filename, exactly as
    in ``services/file_classifier``.
    """

    name = "magic_allowlist"

    def __init__(self, allowed_kinds: frozenset[FileKind] = DEFAULT_ALLOWED_KINDS) -> None:
        self._allowed = allowed_kinds

    async def run(self, ctx: CheckContext) -> CheckResult:
        components: dict[str, dict] = {}
        verdicts: list[str] = []
        for comp in ctx.staged.components:
            content = await comp.read()
            exe = _executable_signature(content)
            if exe is not None:
                components[comp.name] = {"verdict": "block", "reason": exe}
                verdicts.append("block")
                continue
            classified = classify_file(content, comp.name, comp.content_type)
            if classified.kind is FileKind.UNKNOWN:
                components[comp.name] = {"verdict": "fail", "reason": "unclassifiable"}
                verdicts.append("fail")
            elif classified.kind not in self._allowed:
                components[comp.name] = {
                    "verdict": "fail",
                    "reason": f"kind {classified.kind.value!r} not allowed",
                    "kind": classified.kind.value,
                }
                verdicts.append("fail")
            else:
                components[comp.name] = {
                    "verdict": "pass",
                    "kind": classified.kind.value,
                    "mime": classified.mime_type,
                }
                verdicts.append("pass")
        return CheckResult(verdict=aggregate_verdicts(verdicts), details={"components": components})


# async (db, sha256_hex) -> matching item/document ids (as strings).
DedupLookup = Callable[[AsyncSession, str], Awaitable[Sequence[str]]]


class DedupCheck:
    """Flag components whose SHA-256 already exists in the consumer store.

    The lookup is consumer-provided (the engine owns no tables): the
    inbox checks the patient's documents ``original_blob_hash``; the
    public profile checks prior submissions. A hit is a ``warn`` — a
    re-sent referto is routine, the reviewer just shouldn't ingest it
    twice — and the hash lands in the details for the promotion hook to
    reuse (it becomes ``original_blob_hash`` on ingest).
    """

    name = "dedup"

    def __init__(self, lookup: DedupLookup) -> None:
        self._lookup = lookup

    async def run(self, ctx: CheckContext) -> CheckResult:
        components: dict[str, dict] = {}
        verdicts: list[str] = []
        for comp in ctx.staged.components:
            digest = hashlib.sha256(await comp.read()).hexdigest()
            matches = list(await self._lookup(ctx.db, digest))
            entry: dict = {"sha256": digest}
            if matches:
                entry["duplicate_of"] = matches
                verdicts.append("warn")
            else:
                verdicts.append("pass")
            components[comp.name] = entry
        return CheckResult(verdict=aggregate_verdicts(verdicts), details={"components": components})


class DicomRouteCheck:
    """Routing signal, never a gate: mark which components carry a DICOM
    Part-10 preamble so the promotion hook picks the imaging pipeline
    (study ingest) vs the document pipeline for each."""

    name = "dicom_route"

    async def run(self, ctx: CheckContext) -> CheckResult:
        components: dict[str, str] = {}
        dicom_count = 0
        for comp in ctx.staged.components:
            is_dicom = has_dicm_preamble(await comp.read())
            components[comp.name] = "dicom" if is_dicom else "other"
            dicom_count += int(is_dicom)
        return CheckResult(
            verdict="pass",
            details={
                "components": components,
                "dicom_count": dicom_count,
                "route": "dicom" if dicom_count else "document",
            },
        )


class ArchiveGuardCheck:
    """Per-component size cap + archive inspection (zip-slip, zip-bomb,
    member caps) via ``services/archive_guards`` — the same guards the
    bulk-upload unpackers enforce, applied before anything is staged
    deeper into the pipeline. Hostile findings hard-block."""

    name = "archive_guard"

    def __init__(self, max_component_bytes: int = MAX_FILE_BYTES) -> None:
        self._max_component_bytes = max_component_bytes

    async def run(self, ctx: CheckContext) -> CheckResult:
        components: dict[str, dict] = {}
        verdicts: list[str] = []
        for comp in ctx.staged.components:
            if comp.size_bytes > self._max_component_bytes:
                components[comp.name] = {
                    "verdict": "block",
                    "reason": (f"{comp.size_bytes} bytes exceeds cap {self._max_component_bytes}"),
                }
                verdicts.append("block")
                continue
            content = await comp.read()
            if not content.startswith(b"PK\x03\x04"):
                components[comp.name] = {"verdict": "pass"}
                verdicts.append("pass")
                continue
            report = scan_zip_safety(content, max_member_bytes=self._max_component_bytes)
            if report.is_hostile:
                verdict = "block"
            elif report.suspicious:
                verdict = "fail"
            else:
                verdict = "pass"
            components[comp.name] = {
                "verdict": verdict,
                "members": report.member_count,
                "uncompressed": report.total_uncompressed,
                "hostile": list(report.hostile),
                "suspicious": list(report.suspicious),
            }
            verdicts.append(verdict)
        return CheckResult(verdict=aggregate_verdicts(verdicts), details={"components": components})


__all__ = [
    "DEFAULT_ALLOWED_KINDS",
    "ArchiveGuardCheck",
    "ClamAVCheck",
    "DedupCheck",
    "DedupLookup",
    "DicomRouteCheck",
    "MagicAllowlistCheck",
]
