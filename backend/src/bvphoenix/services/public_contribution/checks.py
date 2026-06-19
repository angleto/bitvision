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
from bvphoenix.services.pixel_deid import classify_pixel_risk_bytes
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
    """Burned-in-pixel screening (services.pixel_deid). ``high`` risk -> ``fail``
    so a human MUST review (no automated clearing of high-risk pixels);
    ``none``/``low`` -> ``pass``."""

    name = "pixel_phi"

    async def run(self, ctx: CheckContext) -> CheckResult:
        verdicts: list[str] = []
        components: dict[str, dict] = {}
        for comp in ctx.staged.components:
            risk = classify_pixel_risk_bytes(await comp.read())
            components[comp.name] = {"risk": risk.level, "reasons": list(risk.reasons)}
            verdicts.append("fail" if risk.is_high else "pass")
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
