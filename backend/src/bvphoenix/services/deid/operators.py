"""Project-specific PS3.15 operators: salted consistent UID remap + per-patient
date shift + consistent pseudonyms + age cap + descriptor cleaning.

These are the only stateful pieces of the engine and the reason it is in-house:
linkage is preserved WITHIN a release (same input + same salt -> same output)
but broken across deployments (the secret salt), with no stored mapping table
(a mapping table would itself be a re-identification key).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydicom.multival import MultiValue

_DATE_FMT = "%Y%m%d"


def _hmac_int(salt: str, *parts: str) -> int:
    msg = b"\x00".join(p.encode("utf-8", "replace") for p in parts)
    return int.from_bytes(hmac.new(salt.encode("utf-8"), msg, hashlib.sha256).digest(), "big")


@dataclass
class DeidOperators:
    """Built once per dataset (it needs the original PatientID for the date
    offset). The engine captures the patient key + name tokens before scrubbing
    and hands them here."""

    salt: str
    org_root_uid: str
    date_policy: str = "shift"  # "shift" | "remove"
    patient_key: str = ""  # original PatientID — drives the per-patient date offset
    patient_name: str = ""  # original PatientName — for descriptor cleaning
    redact_terms: tuple[str, ...] = ()  # extra original identifiers to scrub from free text
    _offset_days: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if self.date_policy == "shift" and self.patient_key:
            # Magnitude in [365, 1095] days, sign from an independent bit, so a
            # patient's dates always move >= 1 year and never by zero (which
            # would leak the true date). Same patient -> same offset -> intervals
            # preserved.
            magnitude = 365 + (_hmac_int(self.salt, "dateshift", self.patient_key) % 731)
            sign = -1 if (_hmac_int(self.salt, "datesign", self.patient_key) & 1) else 1
            self._offset_days = sign * magnitude

    @property
    def offset_days(self) -> int:
        return self._offset_days

    def pseudonym(self, value: object) -> str:
        if value in (None, ""):
            return "ANON"
        digest = (
            hashlib.sha256((self.salt + "\x00" + str(value)).encode("utf-8", "replace"))
            .hexdigest()[:12]
            .upper()
        )
        return f"ANON-{digest}"

    def remap_uid(self, value: object):
        if value in (None, ""):
            return value
        if isinstance(value, (MultiValue, list, tuple)):
            return [self.remap_uid(v) for v in value]
        n = _hmac_int(self.salt, "uid", str(value)) % (2**63)
        return f"{self.org_root_uid}.{n}"

    def shift_date(self, value: object, vr: str):
        if self.date_policy == "remove":
            return ""
        if value in (None, ""):
            return value
        if isinstance(value, (MultiValue, list, tuple)):
            return [self.shift_date(v, vr) for v in value]
        if self._offset_days == 0:
            return value
        if vr == "DA":
            return self._shift_da(str(value))
        if vr == "DT":
            return self._shift_dt(str(value))
        return value  # TM: shifting whole days leaves time-of-day unchanged

    def _shift_da(self, s: str) -> str:
        try:
            d = datetime.strptime(s[:8], _DATE_FMT).date() + timedelta(days=self._offset_days)
        except ValueError:
            return ""  # unparseable -> drop rather than leak the original
        return d.strftime(_DATE_FMT)

    def _shift_dt(self, s: str) -> str:
        # DT = YYYYMMDD[HHMMSS[.ffffff]][&ZZXX]; shift only the date head.
        raw = s.strip()
        if len(raw) < 8:
            return ""
        try:
            d = datetime.strptime(raw[:8], _DATE_FMT).date() + timedelta(days=self._offset_days)
        except ValueError:
            return ""
        return d.strftime(_DATE_FMT) + raw[8:]

    def cap_age(self, value: object) -> object:
        # DICOM AS = nnn[DWMY]. Convert ANY unit to years and cap to the 090Y+
        # band above 89 (HIPAA Safe Harbor) — not just the Y form (a >89yo coded
        # as 1200M / 5200W / 36500D must cap too).
        s = str(value or "").strip().upper()
        if len(s) == 4 and s[:3].isdigit():
            n = int(s[:3])
            years = {"Y": n, "M": n // 12, "W": n // 52, "D": n // 365}.get(s[3])
            if years is not None and years > 89:
                return "090Y"
        return value

    def clean_text(self, value: object) -> object:
        if value in (None, ""):
            return value
        # Lazy import: deidentify.py's facade imports the engine, which imports
        # this module — importing deidentify_text at top would cycle.
        from bvphoenix.services.deidentify import deidentify_text

        return deidentify_text(
            str(value),
            patient_name=self.patient_name or None,
            extra_names=self.redact_terms or None,
        )


__all__ = ["DeidOperators"]
