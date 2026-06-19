"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Known-weak JWT secrets that must never appear in production. Kept in one
# place so ``startup_checks`` and the model validator stay in sync.
PLACEHOLDER_JWT_SECRETS: frozenset[str] = frozenset(
    {
        "",
        "dev-only-change-me",
        "changeme",
        "CHANGE_ME",
        "change-me",
        "secret",
        "password",
    }
)


class Settings(BaseSettings):
    """Application settings. Prefix: BVP_."""

    model_config = SettingsConfigDict(
        env_prefix="BVP_",
        env_file=(".env", "../.env"),
        extra="ignore",
    )

    env: str = Field(default="development")
    log_level: str = Field(default="INFO")

    # Comma-separated list of origins permitted to hit the API. Empty in
    # production ⇒ no origin permitted (deny-by-default); ``*`` is only
    # honoured when ``env == "development"``. Production must always set
    # explicit origins.
    cors_origins: str = Field(default="")

    # Comma-separated Host header allow-list for ``TrustedHostMiddleware``.
    # Empty ⇒ disabled (dev). Production should list every hostname the
    # API is served on — rejecting Host-spoofed requests closes a class
    # of cache-poisoning / password-reset-link-forgery attacks.
    trusted_hosts: str = Field(default="")

    # HSTS max-age in seconds. 63072000 = 2 years, the value preload
    # requires. Only emitted when ``env == "production"`` — HSTS on a
    # dev-cert setup traps browsers.
    hsts_max_age: int = Field(default=63072000)

    # Sprint 5b: disk LRU cache for windowed JPEG slices. Default cap
    # 10 GB matches ADR 0012; the cache root defaults to ``/tmp`` so
    # local dev does not need a PVC. Production overrides via
    # ``BVP_SLICE_CACHE_ROOT`` to a persistent volume.
    slice_cache_root: str = Field(default="/tmp/bvp-slice-cache")
    slice_cache_bytes_cap: int = Field(default=10 * 1024**3)

    # Idle window (seconds) for ``audit_session_view`` aggregation
    # (ADR 0005). A read on the same patient by the same actor inside
    # this window bumps the existing row; outside, a new session row
    # is opened. 15 minutes balances forensic granularity and INSERT
    # volume.
    audit_session_window_seconds: int = Field(default=15 * 60)

    database_url: str = Field(
        default="postgresql+asyncpg://bvphoenix:bvphoenix@localhost:5432/bvphoenix"
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg://bvphoenix:bvphoenix@localhost:5432/bvphoenix"
    )
    redis_url: str = Field(default="redis://localhost:6379/0")

    # ---- Review queue (services/review_queue) --------------------------
    # clamd endpoint for the ClamAV auto-check. Empty host ⇒ the check
    # reports ``error`` (an unscanned item is never treated as clean);
    # in production it points at the in-cluster ``clamav`` Service.
    clamd_host: str = Field(default="")
    clamd_port: int = Field(default=3310)
    clamd_timeout_s: float = Field(default=60.0)
    # Comma-separated import paths of the modules that register the
    # review profiles (consumers of the shared engine). The arq worker
    # imports them before resolving a profile by name; empty in
    # deployments where no consumer has landed yet.
    review_profile_modules: str = Field(default="")

    # ---- Patient inbound email inbox (services/inbox) ------------------
    # Master switch: when off, the internal inbound endpoints answer 503
    # and address provisioning is refused — the MTA deployment is
    # pointless without it, and a half-configured deployment must fail
    # closed, not accept mail it cannot store.
    inbound_email_enabled: bool = Field(default=False)
    # The address domain (the MX host's mail domain, NOT the MX host
    # itself): addresses read ``{code}+{tag}@{domain}``.
    inbound_email_domain: str = Field(default="inbox.bitvision.xeno.garden")
    inbound_email_tag: str = Field(default="patient")
    # Entropy of the capability code. 80 bits ⇒ 16 Crockford base32
    # chars; raising it only lengthens new addresses.
    inbound_email_code_bits: int = Field(default=80, ge=40, le=160)
    # Raw ``.eml`` retention; the worker sweep purges older blobs and
    # blanks the subject on the row past this window.
    inbound_email_raw_retention_days: int = Field(default=90, ge=1)
    # Hard cap on one raw message (the MTA advertises it as SMTP SIZE).
    # 50 MiB ≈ the ceiling of mainstream providers; bigger payloads
    # belong to the upload channel.
    inbound_email_max_raw_bytes: int = Field(default=50 * 1024 * 1024)
    # Shared secret the MTA adapter presents on the internal inbound
    # endpoints (``X-Inbound-Key``). Deliberately distinct from
    # ``internal_api_key``: the MTA terminates port 25 on the open
    # Internet — least privilege says its credential must not open the
    # mcp-resolve RPC too. Empty ⇒ endpoints disabled (503).
    inbound_internal_secret: str = Field(default="")
    # Per-address ingestion cap (messages/hour) — the anti-enumeration
    # backstop next to the MTA's own connection limits.
    inbound_email_rate_per_hour: int = Field(default=30, ge=1)

    s3_endpoint_url: str = Field(default="http://localhost:9000")
    # Public-facing endpoint used when signing URLs handed to the
    # browser. When empty we reuse ``s3_endpoint_url`` — correct only if
    # the same hostname resolves from both the backend and the client
    # (e.g. single-machine dev). In Docker, backend talks to MinIO as
    # ``minio:9000`` but the browser needs ``localhost:9000``.
    s3_public_endpoint_url: str = Field(default="")
    s3_region: str = Field(default="us-east-1")
    s3_access_key: str = Field(default="bvphoenix")
    s3_secret_key: str = Field(default="bvphoenix-dev-secret")
    s3_bucket_raw: str = Field(default="bvphoenix-raw")
    s3_bucket_derivatives: str = Field(default="bvphoenix-derivatives")
    # F12.8 cold-tier for entity_objects: when the tier-down worker
    # decides a row should leave Postgres, the canonical bytes are
    # uploaded under this bucket. Reads transparently fall back to S3
    # via ``services.versioning.read_object``.
    s3_bucket_versioning: str = Field(default="bvphoenix-versioning")

    # Concurrency knob for the streaming patient/folder/bulk export
    # pipeline. Each in-flight reader holds one S3 connection from the
    # boto3 pool (capped at 64 in S3Storage) plus one full blob in
    # RAM. With ~1 MB DICOM instances and the worker's 4 GiB pod
    # limit, 32 leaves multiple GiB of headroom. Bump to taste; if
    # you go above 60-ish, also bump ``max_pool_connections`` in
    # ``S3Storage``. Set to ``1`` to disable prefetching entirely.
    export_prefetch_parallelism: int = Field(default=32, ge=1, le=128)

    # Server-side encryption mode for S3 puts. Defaults to AES256 (SSE-S3)
    # which works on AWS and most S3-compatible backends (MinIO >= RELEASE.2021,
    # Cloudflare R2 silently ignores the header). Set to ``aws:kms`` and
    # populate ``s3_kms_key_arn`` to use a customer-managed KMS key. Use
    # ``none`` only for local dev against backends that reject the header.
    s3_encryption: Literal["none", "AES256", "aws:kms"] = Field(default="AES256")
    s3_kms_key_arn: str | None = Field(default=None)

    # Tesseract language tag passed to ``pytesseract`` when the PDF text
    # layer is empty and the rasterised fallback runs. The ``+`` operator
    # asks Tesseract to load multiple traineddata files at once and pick
    # whichever wins per-region: cost is ~linear in the number of langs.
    # The default is conservative (4 langs) to keep the always-loaded
    # working set small; agents that classify a document language ahead
    # of time should pass an explicit ``language=`` override on the API
    # call rather than relying on a wide default.
    #
    # The Dockerfiles install the **full European pack** (24 EU
    # official languages plus Norwegian, Icelandic, Ukrainian, Russian,
    # Turkish, Albanian, Serbian Cyrillic + Latin, Catalan, Welsh,
    # Basque, Galician — the codes Tesseract recognises). Any of those
    # can therefore be passed via ``language=`` without redeploying.
    # Setting a value here that references a language NOT installed
    # crashes Tesseract at startup with "Failed loading language
    # 'xxx'", so any expansion of this default must be matched in the
    # Dockerfiles.
    ocr_languages: str = Field(default="ita+eng+deu+fra")

    oidc_issuer: str = Field(default="")
    oidc_client_id: str = Field(default="")
    oidc_client_secret: str = Field(default="")
    # Callback URL registered at the OIDC provider. Must be reachable by
    # the browser; in dev this is typically http://localhost:8000/api/auth/oidc/callback.
    oidc_redirect_uri: str = Field(default="")

    # Local-JWT auth — used as fallback when OIDC isn't wired (admin
    # bootstrap, dev). BVP_JWT_SECRET has no default: the model validator
    # accepts an empty string in dev (and substitutes an obvious dev
    # marker) but refuses to let the app boot in production without a
    # real secret being supplied via the environment.
    jwt_secret: str = Field(default="")
    jwt_algorithm: str = Field(default="HS256")
    jwt_expires_seconds: int = Field(default=24 * 3600)
    # RFC 7519 ``iss`` (issuer) and ``aud`` (audience) claims. We mint
    # every token with these set and reject any token whose ``aud`` /
    # ``iss`` do not match. The defaults are stable strings so an
    # operator only needs to override them when running a multi-tenant
    # deployment that needs to distinguish staging from prod tokens
    # (otherwise a stolen staging token could replay against prod and
    # vice-versa). Pre-2026-05-21 the local JWT path omitted both
    # claims, which meant a leaked token could be replayed across any
    # environment that happened to share the HS256 secret.
    jwt_issuer: str = Field(default="bvphoenix")
    jwt_audience: str = Field(default="bvphoenix-api")
    # Clock-skew tolerance applied to ``exp``/``nbf`` checks. PyJWT's
    # default is 0 seconds, which makes a 1-second clock drift between
    # backend pods reject a freshly-minted token. 10s is the typical
    # NTP precision on modern Linux and matches the slack we already
    # absorb in OIDC handshakes.
    jwt_leeway_seconds: int = Field(default=10)
    # Optional RS256 support. When ``jwt_algorithm`` is ``RS256`` we sign
    # with the PEM at ``jwt_private_key_path`` and verify with the PEM at
    # ``jwt_public_key_path`` (falls back to the private key which also
    # contains the public part).
    jwt_private_key_path: str = Field(default="")
    jwt_public_key_path: str = Field(default="")

    # MFA enforcement. HIPAA-style deployments require TOTP for admin
    # accounts; flipping this to False is only appropriate for local dev.
    # Regular (non-admin) users can still opt in via /api/mfa/setup.
    require_mfa_for_admin: bool = Field(default=True)

    # PLATFORM_OWNER subject id. The sentinel subject that owns every
    # OpenData fascicolo (anonymised public dataset). Migration 0036
    # seeds the row with this UUID; if you change this in production
    # you must also re-key existing OpenData rows. The default is a
    # well-known UUID stable across deployments. Keep in mind: anyone
    # listed as this subject becomes a global "platform admin" with the
    # power to merge into OpenData mains; treat it as a service identity.
    platform_owner_subject_id: str = Field(default="00000000-0000-0000-0000-000000000099")

    # F12.2d auth tightening: when true, every endpoint that uses
    # ``optional_user`` requires authentication (anonymous access
    # disabled platform-wide). The auth, healthz, and shared-link
    # endpoints stay open by design. Defaults to True per F12 plan
    # ("OpenData accessibile solo agli iscritti"); flip to False for
    # local dev that exercises the legacy anonymous-browse semantics.
    require_auth_globally: bool = Field(default=True)
    # TOTP issuer label shown in authenticator apps.
    mfa_issuer: str = Field(default="bitvision phoenix")

    # LLM provider routing. Resolution order at boot:
    #   - ``stub`` → always StubLLM (used by CI).
    #   - ``anthropic`` / ``openai`` / ``scaleway`` / ``ollama`` →
    #     force that provider (its credentials must be set).
    #   - ``auto`` (default) → first provider with credentials present,
    #     in this preference order: scaleway, anthropic, openai,
    #     ollama. Falls back to StubLLM if none has a key.
    # Pre-2026-05-03 the default was ``stub`` and silently overrode a
    # configured key, surfacing as 502 ``classifier returned invalid
    # JSON`` on the propose_care_phases endpoint.
    llm_provider: str = Field(default="auto")
    llm_default_model: str = Field(default="claude-sonnet-4-6")

    # Per-provider credentials & advisory model defaults. Empty string
    # disables the provider; the resolver in ``services.llm`` consumes
    # them via ``get_settings()``.
    anthropic_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")
    openai_default_model: str = Field(default="gpt-4o-mini")
    scaleway_api_key: str = Field(default="")
    scaleway_default_model: str = Field(default="mistral-small-3.2-24b-instruct-2506")
    scaleway_premium_model: str = Field(default="qwen3-235b-a22b-instruct-2507")
    scaleway_base_url: str = Field(default="https://api.scaleway.ai/v1")
    ollama_base_url: str = Field(default="http://ollama.bvphoenix.svc.cluster.local:11434/v1")
    ollama_default_model: str = Field(default="medgemma:4b")
    ollama_premium_model: str = Field(default="medgemma:27b")
    # Set to ``true`` only when Ollama (or another in-house LLM stack)
    # is actually deployed in the cluster. Until then we keep it false
    # so the in-house rate-card rows stay hidden from the user-facing
    # ``/api/me/ai-models`` dropdown — selecting an undeployed model
    # would surface a confusing 500/404 instead of nothing.
    ollama_enabled: bool = Field(default=False)

    # Master key used to encrypt user-supplied BYOK API keys at rest
    # (F7). 32 bytes as base64url. When empty the BYOK endpoints refuse
    # to accept new keys — production MUST set this. Generate with:
    #   python -c 'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
    byok_master_key: str = Field(default="")

    # Auto-tag worker. Lexicon stage always runs; the LLM stage is
    # gated here to keep the worker free-of-charge by default. When
    # enabled, the worker uses ``anthropic_api_key`` + ``llm_default_model``.
    autotag_use_llm: bool = Field(default=False)

    # Whole-slide imaging upload cap. OpenSlide memory-maps the file
    # at open time, so a giant or malicious WSI can OOM the worker
    # before any application-level validation runs. 30 GiB comfortably
    # accommodates a 100k × 100k 40x SVS scan (~5-15 GiB) plus
    # multi-region NDPI without admitting denial-of-service-grade
    # uploads. Tune up if a future pathology partner ships
    # whole-organ scans.
    wsi_max_bytes: int = Field(default=30 * 1024 * 1024 * 1024)

    @model_validator(mode="after")
    def _reject_weak_jwt_secret_in_production(self) -> Settings:
        """Hard-fail at settings construction if production ships a
        known-placeholder or empty JWT secret. We run this here (not only
        in ``startup_checks``) so even out-of-band importers — migrations,
        scripts — refuse to load with an unsafe config.

        In non-production environments an empty secret is substituted
        with a stable dev-only marker so ``jwt.encode`` has something to
        sign with; production has already been rejected above.
        """
        if self.jwt_algorithm.upper().startswith("HS"):
            if self.env == "production" and self.jwt_secret in PLACEHOLDER_JWT_SECRETS:
                raise RuntimeError(
                    "BVP_JWT_SECRET must be set to a strong random value in production "
                    "(generate with: python -c 'import secrets; print(secrets.token_urlsafe(48))')"
                )
            if not self.jwt_secret:
                # Dev convenience: ``jose`` refuses to sign with an empty
                # secret. Use an obvious marker so a leaked token from a
                # dev box is immediately recognisable.
                self.jwt_secret = "dev-only-insecure-default"
        return self

    # ---- DICOM de-identification (PS3.15 in-house engine) ----
    # Per-deployment secret salt for the keyed-hash UID remap + per-patient
    # date shift (services/deid). Same threat model as jwt_secret: an empty or
    # shared salt would make the remap guessable and linkable ACROSS
    # deployments — defeating the point — so production rejects an empty value.
    deid_secret_salt: str = Field(default="")
    # Org root OID for generated (remapped) UIDs. Default is a documentation
    # OID; a production deployment should set its registered root.
    deid_org_root_uid: str = Field(default="1.2.826.0.1.3680043.10.9999")
    # Bumped whenever the engine's semantics/options change; invalidates the
    # per-study deidentified_at stamp so stale scrubs are re-run.
    deid_method_version: str = Field(default="phoenix-deid-2")
    deid_safe_private_version: str = Field(default="v1")
    # "shift" = Retain Longitudinal Temporal Information (Modified Dates option);
    # "remove" = empty all dates.
    deid_date_policy: str = Field(default="shift")
    deid_clean_descriptors: bool = Field(default=True)
    deid_retain_patient_characteristics: bool = Field(default=True)
    deid_retain_device_identity: bool = Field(default=False)
    deid_retain_safe_private: bool = Field(default=False)

    # ---- Burned-in-pixel VLM hard-case tier (services/pixel_phi_engine, M5) ----
    # Opt-in: off by default, the Tesseract tier + human review are the floor.
    # When on, an OCR-blank high-risk frame is escalated to the engine.
    pixel_phi_vlm_enabled: bool = Field(default=False)
    # In-cluster URL of pixelphi-svc (PaddleOCR + small classifier). Empty +
    # enabled => NullPixelPhiEngine (over-redact uncertain frames, no model).
    pixel_phi_svc_url: str = Field(default="")
    # Host allowlist for the engine: a PHI-bearing crop is NEVER POSTed to a host
    # outside this set (storage isolation, no external API).
    pixel_phi_allowed_hosts: str = Field(default="localhost,127.0.0.1,bvphoenix-pixelphi-svc")

    # ---- Content-safety screening for public contributions (services/content_safety, M6) ----
    # Provider selector: "" / "null" => NullScreener (passes, but records that no
    # screening ran, the absence is visible, never a silent "safe"). "http" =>
    # HttpContentSafetyScreener calling an in-cluster service.
    content_safety_provider: str = Field(default="")
    # In-cluster URL of the screening service (only used when provider == "http").
    content_safety_endpoint: str = Field(default="")
    # Host allowlist: an image is NEVER POSTed to a host outside this set
    # (storage isolation). A configured provider whose host is not allowlisted
    # fails CLOSED to "block" without any network call.
    content_safety_allowed_hosts: str = Field(
        default="localhost,127.0.0.1,bvphoenix-contentsafety-svc"
    )
    content_safety_timeout: float = Field(default=8.0)

    # ---- Burned-in-pixel redaction mode (services/pixel_deid, M6) ----
    # "over_redact" (default, recall-first): mask EVERY detected text box.
    # "selective": keep clinically-relevant scan text (measurements, scale bars),
    # mask only PHI-shaped detections. High-risk still routes to human review in
    # either mode; selective only changes which boxes are blacked out.
    pixel_deid_redaction_mode: str = Field(default="over_redact")

    # ---- De-facing for recognizable-visual-feature risk (services/face_deid, M6) ----
    # Off by default: face-risk (head/face CT/MR/PT) ships as today. When enabled,
    # those instances route through the defacer + human review before any public
    # egress. Real 3D de-facing is a pluggable future tier; the heuristic masker is
    # a conservative placeholder.
    face_deid_enabled: bool = Field(default=False)
    # "null" (records that de-facing did not run -> review) | "heuristic"
    # (conservative anterior-face band mask for HEAD/SKULL/BRAIN only).
    face_deid_mode: str = Field(default="null")

    @model_validator(mode="after")
    def _reject_weak_deid_salt_in_production(self) -> Settings:
        """Mirror the JWT-secret guard for the de-identification salt. An empty
        salt in production would make the keyed-hash UID remap + date shift
        guessable and cross-deployment-linkable; refuse to load. Dev falls back
        to an obvious insecure marker."""
        if self.env == "production" and not self.deid_secret_salt:
            raise RuntimeError(
                "BVP_DEID_SECRET_SALT must be set to a strong random value in production "
                "(generate with: python -c 'import secrets; print(secrets.token_urlsafe(48))')"
            )
        if not self.deid_secret_salt:
            self.deid_secret_salt = "dev-only-insecure-deid-salt"
        return self

    # Email verification. When true, login refuses accounts with a NULL
    # ``email_verified_at``. Default false keeps dev bootstraps usable
    # without a live SMTP relay — production deployments must flip it.
    require_email_verification: bool = Field(default=False)
    email_verification_ttl_seconds: int = Field(default=24 * 3600)
    # Public URL of the frontend; used to build the link mailed to the
    # user. The frontend route /verify-email?token=<raw> consumes it.
    frontend_base_url: str = Field(default="http://localhost:3000")
    # Public URL of the remote MCP HTTP transport. Surfaced to the
    # operator on the AI-assistants page so they know what to paste
    # into Claude.ai's custom-connector dialog. The value mirrors the
    # ``mcp_public_url`` setting on the MCP service itself; phoenix
    # reads its own copy because the MCP HTTP server is reachable from
    # the public Internet but not from the phoenix backend (which
    # would have to traverse a separate ingress).
    mcp_public_url: str = Field(default="https://mcp.bitvision.example/mcp")

    # Shared secret used by sibling services (mcp-http) to call
    # ``POST /api/internal/agent-bearer/resolve``. Empty means the
    # endpoint refuses every request — set in production via the
    # ``bvphoenix-internal`` Secret created by ``create-secrets.sh``.
    internal_api_key: str = Field(default="")

    # SMTP / email sender. ``smtp_host`` empty means dev mode: messages
    # are appended to logs/dev_emails.eml and printed to stdout.
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_use_tls: bool = Field(default=True)
    smtp_from_address: str = Field(default="no-reply@bitvision.local")
    smtp_from_name: str = Field(default="bitvision phoenix")
    # Email delivery. Provider ``stub`` just logs messages; real SMTP /
    # SES wiring can land later without touching call sites.
    email_provider: str = Field(default="stub")
    email_from: str = Field(default="no-reply@bitvision.local")
    # Public origin used to build user-facing links in outbound emails
    # (e.g. the password-reset URL). Falls back to localhost in dev.
    public_frontend_url: str = Field(default="http://localhost:3000")
    # Password-reset token lifetime (minutes). Short by design — the
    # email delivery path is lower-trust than a live session.
    password_reset_ttl_minutes: int = Field(default=15)

    # ---- Notifications dispatcher (sprint C) ---------------------------
    # Global kill-switch. Set false to stop the worker from firing any
    # outbound notification (the post-commit listener still materialises
    # rows in ``notification_dispatches`` for audit, but they stay
    # ``pending`` until the flag flips back). Useful during a feature
    # rollout window or a deliverability incident.
    notifications_enabled: bool = Field(default=True)
    # Fallback reminder offsets when the source event / task does not
    # specify ``reminder_offsets_minutes``. Negative ints = minutes
    # before the anchor. Capped at 5 by the dispatcher.
    notifications_default_offsets: str = Field(default="-1440,-60")
    # Per-channel feature flags. Set false to short-circuit the
    # dispatcher for a channel during a rollout (e.g. WhatsApp business
    # API not approved yet → flag stays false until ops flips it).
    notifications_email_enabled: bool = Field(default=True)
    notifications_webhook_enabled: bool = Field(default=False)
    notifications_telegram_enabled: bool = Field(default=False)
    notifications_whatsapp_enabled: bool = Field(default=False)
    # Telegram bot token (optional). Kept server-side; per-contact only
    # stores the chat_id (no per-contact secret needed, the bot
    # auths the channel itself).
    telegram_bot_token: str = Field(default="")
    # Telegram bot @-handle (no leading @). Used to build the deep-link
    # ``https://t.me/<username>?start=<code>`` for the contact-binding
    # flow. Required alongside the token; the linking endpoint refuses
    # to mint codes when either is empty.
    telegram_bot_username: str = Field(default="")
    # Shared secret echoed by Telegram in every bot webhook update via
    # the ``X-Telegram-Bot-Api-Secret-Token`` header (set via
    # ``setWebhook.secret_token``). Empty → endpoint accepts unsigned
    # updates and logs loudly (dev convenience).
    telegram_webhook_secret: str = Field(default="")
    # HMAC encryption key for ``patient_contacts.webhook_secret_encrypted``.
    # Hex-encoded 32 bytes. pgcrypto's ``pgp_sym_encrypt`` rides on this.
    # Empty in dev → the dispatcher falls back to storing plaintext
    # under the same column (dev-only convenience, logged loudly).
    webhook_encryption_key: str = Field(default="")
    # Timeout for outbound webhook POSTs. Short on purpose: a slow
    # endpoint blocks the worker. Failed deliveries are retried by the
    # arq backoff schedule, not by extending the timeout here.
    webhook_timeout_seconds: int = Field(default=5)
    # Public origin used to build user-facing opt-out / one-click
    # unsubscribe URLs. Falls back to ``public_frontend_url`` so dev
    # works out of the box; production deploys override explicitly to
    # the canonical FE hostname.
    notifications_opt_out_base_url: str = Field(default="")
    # Shared secret for Scaleway TEM (or compatible) bounce webhooks.
    # Empty in dev → the endpoint accepts unsigned requests and logs
    # loudly. Production setup MUST set the secret; mis-signed
    # requests are 401.
    tem_webhook_secret: str = Field(default="")

    # Long-running Jobs (DESIGN.md §11). The per-user cap is the
    # primary DoS guard: without it a single account could spam the
    # ZIP-export endpoint and pin worker slots indefinitely.
    # Idempotency dedup happens *before* the cap check, so retrying
    # the same operation does not consume a slot.
    job_max_active_per_user: int = Field(default=20)
    # Soft global ceiling. Once exceeded, only admins may enqueue;
    # everyone else gets 429. 200 is sized for ~10 concurrent users
    # at the per-user cap; raise if the worker pool grows.
    job_max_active_global: int = Field(default=200)
    # When true, admins are exempt from the per-user cap. Flip to
    # false if a compromised admin account is part of the threat
    # model — admins then share the same 20-slot ceiling.
    job_admin_bypass_cap: bool = Field(default=True)
    # Default result TTL in hours. After this, the cleanup worker
    # prunes the row and deletes its S3 artifact. Per-kind overrides
    # (e.g. shorter for ephemeral derivatives, longer for exports the
    # user might re-download) are configured per consumer.
    job_default_expires_hours: int = Field(default=168)

    # Build identity — baked into the container image at build time
    # via Docker --build-arg and surfaced via GET /api/version so the
    # frontend (and operators) can confirm which release is live.
    # All three are empty strings outside CI; the /api/version handler
    # reports "dev" when the version is empty.
    app_version: str = Field(default="")
    app_git_sha: str = Field(default="")
    app_build_date: str = Field(default="")


@lru_cache
def get_settings() -> Settings:
    return Settings()
