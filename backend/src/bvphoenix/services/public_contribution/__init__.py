"""Public-contribution review profile — the public-egress consumer of the
shared review/staging engine (sibling of ``services.inbox``).

Importing ``profile`` registers the ``public_contribution`` profile
(idempotently). It screens a study offered to the OpenData library (header
de-id, burned-in-pixel risk, malware, CSAM) and gates the publish decision to
human reviewers only.
"""
