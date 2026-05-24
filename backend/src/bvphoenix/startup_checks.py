"""Production safety checks run at application startup.

These validations go beyond the per-field pydantic validators in
``config.py``: they look at combinations of values and at secrets which
are only dangerous when paired with specific deployment modes. Any
failure here raises ``RuntimeError`` so the process refuses to serve
traffic with an insecure configuration.

Only ``env == "production"`` triggers the strict path — development and
test environments keep the permissive defaults that make local work
easy. Tests can exercise the strict path by constructing a ``Settings``
with ``env="production"``.
"""

from __future__ import annotations

from bvphoenix.config import PLACEHOLDER_JWT_SECRETS, Settings

# Minimum acceptable length for an HS256 shared secret. 32 bytes of
# entropy is the floor recommended by RFC 7518 §3.2 for HS256.
_MIN_JWT_SECRET_LEN = 32

# Well-known defaults that are safe for local dev (MinIO bundled in
# docker-compose) but must never reach production.
_DEFAULT_S3_ACCESS_KEYS: frozenset[str] = frozenset({"bvphoenix", "minioadmin"})
_DEFAULT_S3_SECRET_KEYS: frozenset[str] = frozenset({"bvphoenix-dev-secret", "minioadmin"})


def _check_jwt(settings: Settings, errors: list[str]) -> None:
    alg = settings.jwt_algorithm.upper()
    if alg.startswith("HS"):
        secret = settings.jwt_secret
        if secret in PLACEHOLDER_JWT_SECRETS:
            errors.append(
                "BVP_JWT_SECRET is a known placeholder; set it to a strong random value "
                "(e.g. python -c 'import secrets; print(secrets.token_urlsafe(48))')."
            )
        elif len(secret) < _MIN_JWT_SECRET_LEN:
            errors.append(
                f"BVP_JWT_SECRET is too short ({len(secret)} chars); "
                f"require at least {_MIN_JWT_SECRET_LEN} characters of entropy."
            )
    elif alg.startswith("RS") or alg.startswith("ES"):
        if not settings.jwt_private_key_path:
            errors.append(
                f"BVP_JWT_ALGORITHM={settings.jwt_algorithm} requires "
                "BVP_JWT_PRIVATE_KEY_PATH to point at a PEM-encoded private key."
            )
    else:
        errors.append(
            f"Unsupported BVP_JWT_ALGORITHM={settings.jwt_algorithm}; use HS256 or RS256."
        )


def _check_s3(settings: Settings, errors: list[str]) -> None:
    if settings.s3_access_key in _DEFAULT_S3_ACCESS_KEYS:
        errors.append(
            "BVP_S3_ACCESS_KEY is set to a well-known dev default; rotate it in production."
        )
    if settings.s3_secret_key in _DEFAULT_S3_SECRET_KEYS:
        errors.append(
            "BVP_S3_SECRET_KEY is set to a well-known dev default; rotate it in production."
        )


def _check_cors(settings: Settings, errors: list[str]) -> None:
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if not origins or origins == ["*"]:
        errors.append(
            "BVP_CORS_ORIGINS must be an explicit allowlist in production; "
            'wildcard ("*") or empty is not permitted.'
        )


def run_startup_checks(settings: Settings) -> None:
    """Validate the effective configuration. Raise ``RuntimeError`` with
    every violation concatenated into the message (so operators see the
    full list instead of fixing them one at a time)."""
    if settings.env != "production":
        return

    errors: list[str] = []
    _check_jwt(settings, errors)
    _check_s3(settings, errors)
    _check_cors(settings, errors)

    if errors:
        joined = "\n  - ".join(errors)
        raise RuntimeError(
            "Refusing to start in production with insecure configuration:\n  - " + joined
        )
