"""Profile-specific auto-checks for the public-contribution review queue.

These run after the shared structural / malware plugins. They are conservative
by design: high-risk burned-in pixels and any de-id failure route the
submission to mandatory human review (``fail`` -> ``needs_review``); an
unparseable or unscreenable component blocks it. Per the NIH/NCI MIDI-B finding,
no automated method removes 100% of burned-in PHI, so a clean automated pass is
necessary but never sufficient to publish — the human gate is the safety floor.
"""

from __future__ import annotations

from bvphoenix.services.content_safety import get_screener
from bvphoenix.services.deid.errors import DeidVerificationError, RequiresReview
from bvphoenix.services.deidentify import deidentify_dicom_bytes
from bvphoenix.services.face_deid import get_defacer
from bvphoenix.services.pixel_deid import classify_pixel_risk_bytes, clean_pixel_data
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
        defacer = get_defacer()  # None when de-facing is disabled (default)
        verdicts: list[str] = []
        components: dict[str, dict] = {}
        for comp in ctx.staged.components:
            blob = await comp.read()
            risk = classify_pixel_risk_bytes(blob)
            entry: dict = {"risk": risk.level, "reasons": list(risk.reasons)}
            if risk.is_high:
                verdict = "fail"
            elif risk.level == "low" and defacer is not None:
                # Face-risk + de-facing enabled: exercise the de-facer so the
                # reviewer sees the masking outcome, but never auto-pass — the
                # signal a human must confirm before features are declared removed.
                try:
                    res = clean_pixel_data(blob, face_defacer=defacer)
                    entry["face_deid_applied"] = bool(res.redactions)
                    entry["face_deid_reason"] = res.face_deid_reason
                except Exception as exc:  # fail-safe: a de-facer error never auto-passes
                    entry["face_deid_applied"] = False
                    entry["face_deid_reason"] = f"deface_error:{type(exc).__name__}"
                verdict = "fail"
            else:
                verdict = "pass"
            components[comp.name] = entry
            verdicts.append(verdict)
        return CheckResult(
            verdict=aggregate_verdicts(verdicts) if verdicts else "pass",
            details={"components": components},
        )


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
