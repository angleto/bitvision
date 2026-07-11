"""Profile-specific auto-checks for the public-contribution review queue.

These run after the shared structural / malware plugins. They are conservative
by design: high-risk burned-in pixels and any de-id failure route the
submission to mandatory human review (``fail`` -> ``needs_review``); an
unparseable or unscreenable component blocks it. Per the NIH/NCI MIDI-B finding,
no automated method removes 100% of burned-in PHI, so a clean automated pass is
necessary but never sufficient to publish — the human gate is the safety floor.
"""

from __future__ import annotations

import asyncio

from bvphoenix.services.content_safety import get_screener
from bvphoenix.services.deid.errors import DeidVerificationError, RequiresReview
from bvphoenix.services.deidentify import deidentify_dicom_bytes
from bvphoenix.services.face_deid import get_defacer
from bvphoenix.services.pixel_deid import classify_pixel_risk_bytes, clean_pixel_data
from bvphoenix.services.public_contribution.redaction import (
    contrib_staged_prefix,
    stage_component_redaction,
)
from bvphoenix.services.review_queue import CheckContext, CheckResult
from bvphoenix.services.review_queue.checks import aggregate_verdicts


class HeaderDeidCheck:
    """Defense in depth: every DICOM component must survive the PS3.15 header
    engine + its verification. A scrub/verify failure -> ``fail`` (human review);
    an SR/encapsulated requires-review -> ``fail``; an unparseable blob -> ``block``."""

    name = "header_deid"

    async def run(self, ctx: CheckContext) -> CheckResult:
        verdicts: list[str] = []
        components: dict[str, dict] = {}
        for comp in ctx.staged.components:
            data = await comp.read()
            try:
                deidentify_dicom_bytes(data)
                components[comp.name] = {"verdict": "pass"}
                verdicts.append("pass")
            except RequiresReview as exc:
                components[comp.name] = {
                    "verdict": "fail",
                    "reason": "requires_review",
                    "detail": str(exc),
                }
                verdicts.append("fail")
            except DeidVerificationError as exc:
                components[comp.name] = {
                    "verdict": "fail",
                    "reason": "residual_phi",
                    "detail": str(exc),
                }
                verdicts.append("fail")
            except Exception as exc:
                components[comp.name] = {
                    "verdict": "block",
                    "reason": "unparseable",
                    "detail": str(exc),
                }
                verdicts.append("block")
        return CheckResult(
            verdict=aggregate_verdicts(verdicts) if verdicts else "pass",
            details={"components": components},
        )


class PixelPhiCheck:
    """Burned-in-pixel + recognizable-visual-feature screening (services.pixel_deid).

    * ``high`` risk (burned-in text / encapsulated docs) -> ``fail`` so a human
      MUST review (no automated clearing of high-risk pixels).
    * ``none`` -> ``pass``.
    * ``low`` risk is the face / recognizable-visual-feature tier (head/face
      CT/MR/PT). With de-facing disabled (default) it ``pass``es as today. With
      de-facing enabled (``face_deid_enabled``) a face-risk instance is NEVER
      auto-passed: it routes to human review (``fail``), and the de-facer is
      exercised so the reviewer sees whether a mask was applied
      (``face_deid_applied``) and why (``face_deid_reason``). The heuristic
      masker is not validated de-facing, so the verdict is ``fail`` even when a
      mask was applied; ``RecognizableVisualFeatures=NO`` + CID 7050 ``113102``
      are stamped only after a human accepts, via
      ``pixel_deid.mark_visual_features_removed``.
    """

    name = "pixel_phi"

    async def run(self, ctx: CheckContext) -> CheckResult:
        from bvphoenix.storage import get_s3_storage

        defacer = get_defacer()  # None when de-facing is disabled (default)
        verdicts: list[str] = []
        components: dict[str, dict] = {}

        # Pair each staged component with its manifest instance entry so the
        # staged redaction can be recorded per instance. The profile's
        # ``load_staged`` builds components from exactly the manifest entries
        # that carry an ``s3_key``, in order — a length mismatch means we are
        # not looking at a real submission (unit-test fixtures pass a bare
        # manifest), so staging is skipped and only classification runs.
        manifest = dict(ctx.staged.manifest or {})
        keyed_entries = [i for i in manifest.get("instances", []) if i.get("s3_key")]
        can_stage = ctx.db is not None and len(keyed_entries) == len(ctx.staged.components)
        # Updates keyed by position within ``keyed_entries`` (names are not
        # guaranteed unique across series; the 1:1 component order is).
        updated_by_idx: dict[int, dict] = {}

        for idx, comp in enumerate(ctx.staged.components):
            blob = await comp.read()
            risk = classify_pixel_risk_bytes(blob)
            entry: dict = {"risk": risk.level, "reasons": list(risk.reasons)}
            needs_gate = risk.is_high or (risk.level == "low" and defacer is not None)
            m_entry = keyed_entries[idx] if can_stage else None
            if needs_gate and m_entry is not None and m_entry.get("instance_id"):
                # Stage the redacted rendition NOW: the reviewer previews these
                # exact bytes and the promote hook publishes them (what you
                # review is what ships). Deterministic key -> re-runs overwrite.
                staged = await asyncio.to_thread(
                    stage_component_redaction,
                    get_s3_storage(),
                    submission_id=ctx.staged.item_id,
                    instance_id=str(m_entry["instance_id"]),
                    raw=blob,
                    defacer=defacer,
                )
                entry["staged"] = staged.key is not None
                if staged.reason:
                    entry["staged_reason"] = staged.reason
                if risk.level == "low":
                    entry["face_deid_applied"] = staged.face_deid_applied
                    entry["face_deid_reason"] = staged.face_deid_reason
                # Re-run overwrite: strip any stale staged_* audit before merging.
                base = {k: v for k, v in m_entry.items() if not k.startswith("staged_")}
                updated_by_idx[idx] = {
                    **base,
                    "risk_level": risk.level,
                    "staged_redacted_key": staged.key,
                    "staged_sha256": staged.sha256,
                    "staged_residual": staged.residual_suspect,
                    "staged_reason": staged.reason,
                    "staged_redactions": staged.redactions,
                    "staged_redactions_truncated": staged.redactions_truncated,
                    "staged_deid_version": staged.deid_method_version,
                    "face_deid_applied": staged.face_deid_applied,
                    "face_deid_reason": staged.face_deid_reason,
                }
                verdict = "fail"
            elif needs_gate:
                if risk.level == "low":
                    # Face-risk + de-facing enabled but no manifest entry to
                    # stage against: still exercise the de-facer so the reviewer
                    # sees the masking outcome. Never auto-pass.
                    try:
                        res = clean_pixel_data(blob, face_defacer=defacer)
                        entry["face_deid_applied"] = bool(res.redactions)
                        entry["face_deid_reason"] = res.face_deid_reason
                    except Exception as exc:  # a de-facer error never auto-passes
                        entry["face_deid_applied"] = False
                        entry["face_deid_reason"] = f"deface_error:{type(exc).__name__}"
                verdict = "fail"
            else:
                verdict = "pass"
            components[comp.name] = entry
            verdicts.append(verdict)

        if updated_by_idx:
            await self._persist_staged(ctx, manifest, updated_by_idx)

        return CheckResult(
            verdict=aggregate_verdicts(verdicts) if verdicts else "pass",
            details={"components": components},
        )

    @staticmethod
    async def _persist_staged(
        ctx: CheckContext, manifest: dict, updated_by_idx: dict[int, dict]
    ) -> None:
        """Write the staged-redaction audit back onto the Submission row.

        ``updated_by_idx`` is keyed by position within the s3_key-carrying
        entries (the component order). The manifest is reassigned as a NEW dict
        (SQLAlchemy JSONB change detection does not see in-place mutation); the
        surrounding engine transition + the worker's commit persist it
        atomically with the verdict."""
        from bvphoenix.db.models import Submission

        sub = await ctx.db.get(Submission, ctx.staged.item_id)
        if sub is None:  # pragma: no cover - the engine loaded this row moments ago
            return
        instances: list[dict] = []
        k = 0
        for entry in manifest.get("instances", []):
            if entry.get("s3_key"):
                instances.append(updated_by_idx.get(k, entry))
                k += 1
            else:
                instances.append(entry)
        sub.manifest = {**manifest, "instances": instances}
        if any(i.get("staged_redacted_key") for i in instances):
            sub.staged_prefix = contrib_staged_prefix(sub.id)


class CsamScreenCheck:
    """CSAM / NSFW screening seam (services.content_safety). A positive hit
    ``block``s the submission (no overridable accept). With no provider
    configured the null screener passes but records that no screening ran."""

    name = "csam_screen"

    async def run(self, ctx: CheckContext) -> CheckResult:
        screener = get_screener()
        verdicts: list[str] = []
        components: dict[str, dict] = {}
        for comp in ctx.staged.components:
            res = await screener.screen(await comp.read())
            components[comp.name] = {"verdict": res.verdict, "provider": res.provider}
            verdicts.append(res.verdict if res.verdict in ("block", "error") else "pass")
        return CheckResult(
            verdict=aggregate_verdicts(verdicts) if verdicts else "pass",
            details={"components": components},
        )


__all__ = ["CsamScreenCheck", "HeaderDeidCheck", "PixelPhiCheck"]
