"""Unit tests for the calendar-subscription HMAC token.

Pure (no DB, not gated): the token is the cryptographic half of the
public-feed credential. These pin the invariants the feed endpoint
relies on:

* round-trips the exact (subscription_id, patient_id) pair;
* any bit flipped anywhere -> rejected (constant-time MAC);
* path-safe (no ``.``, no ``/``, no ``+``) so it slots into
  ``/calendar/feed/{token}.ics``;
* the patient id is bound by the signature — you cannot mutate the
  embedded patient and still verify (cross-patient inexpressible).
"""

from __future__ import annotations

import base64
import uuid

from bvphoenix.services.calendar_subscription_token import sign, verify


def test_roundtrip_returns_exact_pair() -> None:
    sid, pid = uuid.uuid4(), uuid.uuid4()
    token = sign(sid, pid)
    assert verify(token) == (sid, pid)


def test_token_is_path_safe() -> None:
    token = sign(uuid.uuid4(), uuid.uuid4())
    assert "." not in token  # would collide with the .ics suffix
    assert "/" not in token
    assert "+" not in token
    assert "=" not in token  # padding stripped


def test_distinct_pairs_distinct_tokens() -> None:
    sid = uuid.uuid4()
    a = sign(sid, uuid.uuid4())
    b = sign(sid, uuid.uuid4())
    assert a != b


def test_deterministic() -> None:
    sid, pid = uuid.uuid4(), uuid.uuid4()
    assert sign(sid, pid) == sign(sid, pid)


def test_tamper_anywhere_rejected() -> None:
    """Any single-bit flip in the decoded byte stream is rejected.

    Mutating the base64url *string* would be flaky: the last character
    of an unpadded base64url encoding carries fewer than 6 meaningful
    bits, so two distinct chars can decode to the same byte sequence
    (the HMAC stays valid and the test would falsely accept). We
    mutate the underlying bytes instead — every byte position is then
    a real change.
    """
    token = sign(uuid.uuid4(), uuid.uuid4())
    raw = _b64url_decode(token)
    for i in range(len(raw)):
        mutated_bytes = bytearray(raw)
        mutated_bytes[i] ^= 0x01
        mutated_token = _b64url_encode(bytes(mutated_bytes))
        assert verify(mutated_token) is None, f"accepted a token mutated at byte index {i}"


def test_garbage_rejected() -> None:
    assert verify("") is None
    assert verify("not-base64-!!!") is None
    assert verify("aGVsbG8") is None  # valid b64, wrong length
    assert verify("x" * 200) is None


def test_patient_id_is_bound_by_signature() -> None:
    """Swapping the embedded patient id breaks the MAC: an attacker who
    knows the URL structure still cannot retarget it to another
    fascicolo without the server secret."""
    sid, pid = uuid.uuid4(), uuid.uuid4()
    token = sign(sid, pid)
    raw = bytearray(_b64url_decode(token))
    # patient id occupies bytes 17..33; flip one of its bytes.
    raw[20] ^= 0x01
    forged = _b64url_encode(bytes(raw))
    assert verify(forged) is None


# Local copies of the codec so the test does not reach into private
# helpers of the module under test (keeps the contract at sign/verify).
def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
