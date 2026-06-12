"""Patient inbound inbox (task fbbf5270).

The patient-private consumer of the shared review/staging engine
(``services/review_queue``): capability e-mail addresses, raw-message
intake, MIME staging, the ``patient_inbox`` review profile and the
promotion path into the fascicolo.

Modules:

* ``codes``     — Crockford base32 capability codes;
* ``addresses`` — address provisioning / revocation / lookup;
* ``mime``      — robust RFC 5322/2047 parsing, attachment extraction;
* ``emails``    — raw intake (S3 + ``inbound_emails``) and staging of
  one reviewable ``InboxItem`` per message;
* ``checks``    — profile-specific auto-checks (sender verification);
* ``policy``    — ``should_require_review`` (which ingress channels go
  through the queue);
* ``profile``   — the registered :class:`ReviewProfile`;
* ``promotion`` — the accept hook (DICOM → study ingest, anything else
  → ``ingest_document_blob``; held uploads → enqueue) and the reject
  purge.
"""
