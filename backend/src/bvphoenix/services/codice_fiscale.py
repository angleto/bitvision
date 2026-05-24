"""Decoder + sanity-checker for the Italian codice fiscale.

The CF (16-char) encodes:

* ``surname[3] + first_name[3]`` — three consonants, padded with vowels
  / X. Not reversible (Rossi, Rosci, Russi all map to ``RSS``), so we
  surface no surname/first-name decoding here.
* ``year[2] + month[1] + day[2]`` — birth date. Month is encoded as a
  letter ``A``..``H``, ``L``..``T`` (skipping I, J, K). Day is the
  numeric day for males; ``day + 40`` for females (so ``59`` for a
  woman born on the 19th).
* ``belfiore[4]`` — comune-of-birth code (``H501`` = Roma, ``F205`` =
  Milano, …). Foreign-born patients use a ``Z`` + 3-digit code
  (``Z602`` = Romania).
* ``check[1]`` — Luhn-style control char. We don't enforce it here; a
  CF that fails the check digit shows up via the regex of the existing
  audit-scrub logic (``services/audit.py``).

What we expose
--------------

``decode_codice_fiscale(cf) -> DecodedCF | None``
    Pure function. Returns ``None`` for syntactically invalid inputs
    (wrong length / unknown month letter) so callers can branch
    cleanly. Returned ``birth_date`` may be ``None`` when the CF is
    one of the legacy "temporary" forms with the year ``00`` / day
    ``00``.

``check_consistency(cf, *, birth_date, sex, birth_place_belfiore=None)``
    Returns a list of ``Mismatch`` entries describing each
    disagreement. **Never** auto-rewrites the input. Callers (the
    PATCH endpoint, the MCP tool dry-run preview) decide what to do
    with the warnings — typically forward them to the human as
    "double-check this".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final

# Month letter → 1..12. Skips I, J, K (legacy reasons; same convention
# the Italian Ministry of Finance uses on every CF since 1976).
_MONTH_LETTERS: Final[dict[str, int]] = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "H": 6,
    "L": 7,
    "M": 8,
    "P": 9,
    "R": 10,
    "S": 11,
    "T": 12,
}

_FEMALE_DAY_OFFSET: Final[int] = 40
_CF_LENGTH: Final[int] = 16


@dataclass(slots=True)
class DecodedCF:
    """Demographic fields decoded from a syntactically valid CF.

    ``birth_date`` rolls the 2-digit year against today's century (so
    ``58`` decodes to ``1958`` if we're after 2026, otherwise it would
    overshoot; clinical patients are virtually never < 1925 so the
    100-year rolling window is safe).
    """

    surname_initials: str
    first_name_initials: str
    birth_date: date | None
    sex: str  # "M" or "F"
    birth_place_belfiore: str  # 4-char Belfiore code; ``Z***`` for foreign
    is_foreign_born: bool


@dataclass(slots=True)
class Mismatch:
    """One disagreement between a CF claim and a stored field.

    ``field`` is the patient column name (``birth_date``, ``sex``, …);
    ``stored`` is what the row currently carries; ``decoded_from_cf``
    is what the CF would imply.
    """

    field: str
    stored: object | None
    decoded_from_cf: object | None
    detail: str


def _resolve_year(yy: int) -> int:
    """Map a 2-digit year to a full year using a 100-year sliding window
    centred on today. ``58`` two years from 2026 reads as 1958, not
    2058; ``05`` reads as 2005."""
    today = date.today()
    century = (today.year // 100) * 100
    candidate = century + yy
    if candidate > today.year:
        candidate -= 100
    return candidate


def decode_codice_fiscale(cf: str | None) -> DecodedCF | None:
    """Pure, regex-free decoder.

    Returns ``None`` when the input is missing, the wrong length, or
    carries a month letter outside the official set. Surface-level
    only: does **not** validate the check digit, does **not** look up
    the Belfiore code in any table — both would require extra data
    files we don't ship.
    """
    if not cf:
        return None
    cf = cf.strip().upper()
    if len(cf) != _CF_LENGTH:
        return None

    surname_initials = cf[0:3]
    first_name_initials = cf[3:6]
    try:
        yy = int(cf[6:8])
    except ValueError:
        return None
    month_letter = cf[8]
    month = _MONTH_LETTERS.get(month_letter)
    if month is None:
        return None
    try:
        dd = int(cf[9:11])
    except ValueError:
        return None
    sex: str
    if dd >= _FEMALE_DAY_OFFSET + 1:
        sex = "F"
        day = dd - _FEMALE_DAY_OFFSET
    else:
        sex = "M"
        day = dd

    birth_date: date | None
    if day == 0 or yy < 0 or yy > 99:
        # Legacy "placeholder" CFs use day=0 / impossible years; surface
        # them as decoded-but-no-date rather than crash.
        birth_date = None
    else:
        try:
            birth_date = date(_resolve_year(yy), month, day)
        except ValueError:
            birth_date = None

    belfiore = cf[11:15]
    is_foreign = belfiore.startswith("Z")

    return DecodedCF(
        surname_initials=surname_initials,
        first_name_initials=first_name_initials,
        birth_date=birth_date,
        sex=sex,
        birth_place_belfiore=belfiore,
        is_foreign_born=is_foreign,
    )


def check_consistency(
    cf: str | None,
    *,
    birth_date: date | None,
    sex: str | None,
    birth_place_belfiore: str | None = None,
) -> list[Mismatch]:
    """Compare a CF against the stored demographic fields.

    Returns an empty list when the CF is missing or unparseable —
    silence is the right behaviour because we can't conclude anything.
    Each ``Mismatch`` describes one disagreement; the caller decides
    whether to display them, block the write, or do nothing.

    ``birth_place_belfiore`` is the 4-char code stored on the patient
    when the deployment carries a Belfiore lookup table (we don't
    ship one, so the field is usually ``None`` and the check is a
    no-op for that field).
    """
    decoded = decode_codice_fiscale(cf)
    if decoded is None:
        return []

    out: list[Mismatch] = []

    if (
        decoded.birth_date is not None
        and birth_date is not None
        and decoded.birth_date != birth_date
    ):
        out.append(
            Mismatch(
                field="birth_date",
                stored=birth_date.isoformat(),
                decoded_from_cf=decoded.birth_date.isoformat(),
                detail=(
                    "Stored birth_date does not match the date encoded in "
                    "the codice fiscale. Verify which one is correct: a CF "
                    "is hard to change, but a typo on the demographic form "
                    "is common."
                ),
            )
        )

    if sex and decoded.sex and sex.upper() != decoded.sex:
        out.append(
            Mismatch(
                field="sex",
                stored=sex,
                decoded_from_cf=decoded.sex,
                detail=(
                    "Stored sex does not match the codice fiscale. CF "
                    "encodes sex via the day field (>40 = F)."
                ),
            )
        )

    if (
        birth_place_belfiore
        and decoded.birth_place_belfiore
        and birth_place_belfiore.upper() != decoded.birth_place_belfiore
    ):
        out.append(
            Mismatch(
                field="birth_place_belfiore",
                stored=birth_place_belfiore,
                decoded_from_cf=decoded.birth_place_belfiore,
                detail=(
                    "Stored Belfiore code differs from the one in the CF. "
                    "Sometimes the patient was registered with a frazione "
                    "code while the CF carries the parent comune."
                ),
            )
        )

    return out


__all__ = ["DecodedCF", "Mismatch", "check_consistency", "decode_codice_fiscale"]
