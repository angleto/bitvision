"""License helpers for the OpenData tier.

A single place to decide whether an SPDX license permits commercial reuse,
shared by every response schema that surfaces a public-dataset license
(``StudyOut``, ``PathologySlideOut``). The platform has commercial intent,
so CC-BY / CC0 data is commercial-OK while CC-BY-NC-* is retained but
labelled non-commercial / educational use only and excluded from any
commercial tier.
"""

from __future__ import annotations


def license_allows_commercial_use(license_spdx: str | None) -> bool:
    """True unless the SPDX id carries a NonCommercial (``-NC``) clause.

    CC-BY-3.0/4.0 and CC0-1.0 permit commercial reuse → True. CC-BY-NC-*
    (and the NC-SA viral variants) → False. ``None`` (private / unlicensed
    study) → True: the flag is only meaningful where a license is present,
    and the license badge is hidden client-side when ``license_spdx`` is
    null, so the default never mislabels a private study.
    """
    if not license_spdx:
        return True
    return "-NC" not in license_spdx.upper()
