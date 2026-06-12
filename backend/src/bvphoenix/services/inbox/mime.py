"""MIME parsing for inbound messages — stdlib ``email``, defensive.

Runs in the worker, never in the MTA or the HTTP handler: a hostile
message must at worst fail one arq job. Built on
``email.message_from_bytes(policy=email.policy.default)`` which gives
us RFC 2047 header decoding, charset handling and ``iter_attachments``
for free, instead of re-implementing multipart traversal.

What counts as an attachment: parts with ``Content-Disposition:
attachment``, plus inline parts that carry a filename and are not the
message body (scanners and some hospital gateways send the PDF inline).
Signature images and tracking pixels are dropped by a minimum-size
floor rather than content sniffing — the auto-checks downstream judge
content, this stage only decides *what is worth staging*.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage

# Below this size an inline image is virtually always a signature
# logo / tracking pixel. Attachment-disposition parts are kept at any
# size (an explicit attachment is an explicit intent).
MIN_INLINE_ATTACHMENT_BYTES = 10 * 1024

# Per-message component cap: a hostile message must not explode one
# review item into thousands of manifest entries.
MAX_COMPONENTS = 100

_AUTH_RESULT_RE = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-zA-Z0-9_-]+)")


@dataclass(frozen=True, slots=True)
class ParsedAttachment:
    filename: str
    content_type: str | None
    payload: bytes


@dataclass(frozen=True, slots=True)
class ParsedEmail:
    message_id: str | None
    from_address: str | None
    to_address: str | None
    subject: str | None
    date: datetime | None
    spf_result: str | None
    dkim_result: str | None
    dmarc_result: str | None
    body_text: str | None
    is_auto_submitted: bool
    attachments: list[ParsedAttachment] = field(default_factory=list)


def _first_address(value: str | None) -> str | None:
    if not value:
        return None
    name_addr = email.utils.getaddresses([value])
    for _name, addr in name_addr:
        if addr:
            return addr.lower()[:320]
    return None


def _auth_results(msg: EmailMessage) -> dict[str, str]:
    """Collapse ``Authentication-Results`` headers into spf/dkim/dmarc.

    The MTA does not verify signatures itself (it is a dumb adapter);
    these are the results stamped by whatever upstream hop did, and are
    treated strictly as *signals* downstream. First occurrence wins —
    the topmost header is the closest, most trustworthy hop.
    """
    results: dict[str, str] = {}
    for header in msg.get_all("Authentication-Results", []):
        for mech, outcome in _AUTH_RESULT_RE.findall(str(header)):
            results.setdefault(mech.lower(), outcome.lower()[:16])
    return results


def _decode_filename(part: EmailMessage, index: int) -> str:
    raw = part.get_filename()
    if not raw:
        ext = (part.get_content_subtype() or "bin").lower()
        return f"attachment-{index}.{ext}"
    # Strip any path component an attacker smuggles into the filename;
    # the staging key builder treats this as a single S3 path segment.
    cleaned = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return cleaned[:255] or f"attachment-{index}.bin"


def _body_text(msg: EmailMessage) -> str | None:
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        return None
    if body is None:
        return None
    try:
        content = body.get_content()
    except Exception:
        return None
    if not isinstance(content, str):
        return None
    return content.strip() or None


def parse_inbound_email(raw: bytes) -> ParsedEmail:
    """Parse a raw RFC 5322 message into envelope metadata + staged-worthy
    attachments. Never raises on malformed input: the stdlib parser
    degrades to defects, and a message we cannot make sense of simply
    yields zero attachments (the item still queues, the reviewer sees
    the raw)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    attachments: list[ParsedAttachment] = []
    index = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        if disposition != "attachment" and not filename:
            continue  # body / structural part
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if not payload or not isinstance(payload, bytes):
            continue
        if disposition != "attachment" and len(payload) < MIN_INLINE_ATTACHMENT_BYTES:
            continue  # inline signature image / pixel
        index += 1
        attachments.append(
            ParsedAttachment(
                filename=_decode_filename(part, index),
                content_type=part.get_content_type(),
                payload=payload,
            )
        )
        if len(attachments) >= MAX_COMPONENTS:
            break

    auth = _auth_results(msg)
    date_header = msg.get("Date")
    parsed_date: datetime | None = None
    if date_header:
        try:
            parsed_date = email.utils.parsedate_to_datetime(str(date_header))
        except (TypeError, ValueError):
            parsed_date = None

    auto_submitted = str(msg.get("Auto-Submitted", "")).strip().lower()
    precedence = str(msg.get("Precedence", "")).strip().lower()
    is_auto = (auto_submitted not in ("", "no")) or precedence in ("bulk", "junk", "list")

    message_id = msg.get("Message-ID")
    return ParsedEmail(
        message_id=str(message_id).strip()[:998] if message_id else None,
        from_address=_first_address(msg.get("From")),
        to_address=_first_address(msg.get("To")),
        subject=str(msg.get("Subject", "")).strip()[:2000] or None,
        date=parsed_date,
        spf_result=auth.get("spf"),
        dkim_result=auth.get("dkim"),
        dmarc_result=auth.get("dmarc"),
        body_text=_body_text(msg),
        is_auto_submitted=is_auto,
        attachments=attachments,
    )


__all__ = [
    "MAX_COMPONENTS",
    "MIN_INLINE_ATTACHMENT_BYTES",
    "ParsedAttachment",
    "ParsedEmail",
    "parse_inbound_email",
]
