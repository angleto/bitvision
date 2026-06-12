"""MTA adapter settings (env prefix ``BVP_``, shared with the backend).

The adapter holds exactly two pieces of trust: where the backend is
(in-cluster ClusterIP URL) and the inbound shared secret. Everything
else is plumbing — bind address, advertised hostname, size limit and
the optional STARTTLS cert pair.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BVP_", extra="ignore")

    # In-cluster backend base URL (the internal endpoints are stripped
    # at the public ingress, so this MUST be the ClusterIP service).
    mta_backend_url: str = Field(default="http://localhost:8000")
    # Shared secret presented as ``X-Inbound-Key`` — mirrors the
    # backend's ``BVP_INBOUND_INTERNAL_SECRET``. Empty ⇒ the adapter
    # refuses to start (fail closed).
    inbound_internal_secret: str = Field(default="")

    mta_bind_host: str = Field(default="0.0.0.0")
    mta_bind_port: int = Field(default=2525)
    # EHLO hostname (the MX record target).
    mta_hostname: str = Field(default="mx.bitvision.xeno.garden")
    # Mirrors backend ``inbound_email_max_raw_bytes`` (advertised as
    # SMTP SIZE; aiosmtpd enforces it during DATA).
    inbound_email_max_raw_bytes: int = Field(default=50 * 1024 * 1024)
    # Optional STARTTLS cert/key (PEM paths). Both set ⇒ STARTTLS is
    # offered (opportunistic, never required: inbound mail from
    # legacy relays must still flow).
    mta_tls_cert_file: str = Field(default="")
    mta_tls_key_file: str = Field(default="")
    # Per-request timeout towards the backend. RCPT validation is a
    # point lookup; DATA forwarding scales with message size.
    mta_rcpt_timeout_s: float = Field(default=10.0)
    mta_data_timeout_s: float = Field(default=120.0)


@lru_cache
def get_settings() -> Settings:
    return Settings()


__all__ = ["Settings", "get_settings"]
