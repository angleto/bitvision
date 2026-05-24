"""FastAPI application entry point."""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.requests import Request as _StarletteRequest

from bvphoenix import __version__
from bvphoenix.api import api_router
from bvphoenix.api.a2a import AGENT_CARD
from bvphoenix.config import get_settings
from bvphoenix.logging import install_phi_redaction
from bvphoenix.middleware.problem_details import install_problem_details
from bvphoenix.services.rate_limit import limiter
from bvphoenix.startup_checks import run_startup_checks

# Raise Starlette's per-request multipart limits so a real CD with a
# multi-thousand-instance MRI / CT study can be uploaded in one shot.
# Starlette's ``Request.form()`` defaults to ``max_files=1000`` /
# ``max_fields=1000`` and FastAPI calls it without overrides when
# resolving ``File(...)`` / ``Form(...)`` dependencies. A 320-slice CT
# abdomen with 8 series, or a multi-protocol MRI exam, easily blows
# past 1000 instances; the parallel ``relative_paths[]`` array makes
# ``max_fields`` blow at the same threshold. We monkey-patch the
# method so every dependency-driven call inherits the larger ceiling.
# ``max_part_size`` (per-part bytes) is left at Starlette's default;
# per-file caps are enforced later by the upload endpoints themselves.
_BVP_MAX_MULTIPART_FILES = 50_000
_BVP_MAX_MULTIPART_FIELDS = 50_000

_orig_request_form = _StarletteRequest.form

# Also widen the Starlette MultiPartParser ceilings: a raw DICOM slice
# can comfortably exceed 1 MB (modern CT / MR / DX / MG) and the
# default ``max_part_size = 1 MiB`` would surface as a generic
# "There was an error parsing the body" 400 to the user. The per-file
# byte cap (500 MB) is enforced later, by the upload endpoints.
import starlette.formparsers as _starlette_formparsers

# Multi-frame DICOM (enhanced CT / MR / US) routinely packs an entire
# series into a single SOP instance — easily tens of MB, occasionally
# hundreds. Setting the parser ceiling at 100 MB covers the common
# clinical case; truly oversized files are still bounded by the
# per-endpoint ``MAX_FILE_BYTES`` cap (500 MB) inside the upload
# handlers themselves.
_BVP_MAX_PART_BYTES = 100 * 1024 * 1024
_orig_multipart_init = _starlette_formparsers.MultiPartParser.__init__


def _patched_multipart_init(
    self,
    headers,
    stream,
    *,
    max_files: int | float = _BVP_MAX_MULTIPART_FILES,
    max_fields: int | float = _BVP_MAX_MULTIPART_FIELDS,
    max_part_size: int = _BVP_MAX_PART_BYTES,
):
    _orig_multipart_init(
        self,
        headers,
        stream,
        max_files=max_files,
        max_fields=max_fields,
        max_part_size=max_part_size,
    )


_starlette_formparsers.MultiPartParser.__init__ = _patched_multipart_init


def _patched_request_form(
    self,
    *,
    max_files: int | float = _BVP_MAX_MULTIPART_FILES,
    max_fields: int | float = _BVP_MAX_MULTIPART_FIELDS,
    max_part_size: int = _BVP_MAX_PART_BYTES,
):
    return _orig_request_form(
        self,
        max_files=max_files,
        max_fields=max_fields,
        max_part_size=max_part_size,
    )


_StarletteRequest.form = _patched_request_form  # type: ignore[method-assign]

settings = get_settings()
# Fail fast on insecure configuration before any request is served.
run_startup_checks(settings)

# Install PHI redaction before any downstream module can emit a log
# record. Uvicorn / SQLAlchemy / app loggers all route through the root
# filter from this point forward.
install_phi_redaction(level=settings.log_level)

app = FastAPI(
    title="bitvision phoenix",
    description="REST API for the bitvision phoenix medical imaging platform.",
    version=__version__,
    docs_url="/docs",
    openapi_url="/openapi.json",
)

# slowapi requires the Limiter on app.state — decorators resolve it via
# request.app.state.limiter when a decorated route is invoked.
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.on_event("startup")
async def _bootstrap_llm_rate_cards() -> None:
    """Pull the active ``llm_rate_cards`` rows into the in-memory
    override cache so the very first ``/ask`` after a fresh pod uses
    the operator's edited prices, not the static defaults baked into
    the image. Failure here is non-fatal (the static fallback still
    answers): we only log a warning."""
    import logging

    from bvphoenix.db.session import SessionFactory
    from bvphoenix.services.llm_rate_cards import refresh_rate_cards

    log = logging.getLogger("bvphoenix.startup")
    try:
        async with SessionFactory() as db:
            n = await refresh_rate_cards(db)
        log.info("llm_rate_cards bootstrap loaded %d active rows", n)
    except Exception as exc:
        log.warning("llm_rate_cards bootstrap skipped: %s", exc)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(_request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"rate limit exceeded: {exc.detail}"},
    )


_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()] or ["*"]
_is_production = settings.env.lower() == "production"
_configured_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if _configured_origins:
    _origins = _configured_origins
elif _is_production:
    # Deny-by-default: a misconfigured prod deployment refuses CORS rather
    # than echoing ``*``. Operators must set BVP_CORS_ORIGINS explicitly.
    _origins = []
else:
    _origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_trusted = [h.strip() for h in settings.trusted_hosts.split(",") if h.strip()]
if _trusted:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_trusted)


class SecurityHeadersMiddleware:
    """Attach baseline security headers to every response.

    Implemented as a pure ASGI middleware to avoid the extra task-group
    overhead of ``BaseHTTPMiddleware`` — this runs on every response.
    HSTS is production-only: emitting ``Strict-Transport-Security`` on a
    dev-cert setup can trap developers' browsers into forcing HTTPS on
    ``localhost``.
    """

    def __init__(self, app, *, production: bool, hsts_max_age: int) -> None:
        self.app = app
        extra: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
        ]
        if production:
            extra.append(
                (
                    b"strict-transport-security",
                    f"max-age={hsts_max_age}; includeSubDomains".encode(),
                )
            )
        self._extra = extra

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name for name, _ in headers}
                for name, value in self._extra:
                    if name not in present:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(
    SecurityHeadersMiddleware,
    production=_is_production,
    hsts_max_age=settings.hsts_max_age,
)

install_problem_details(app)

app.include_router(api_router)


@app.get("/health", tags=["infra"])
async def health() -> dict[str, str]:
    """Liveness probe. Does not verify downstream dependencies."""
    return {"status": "ok", "version": __version__, "env": settings.env}


@app.get("/api/system/features", tags=["infra"])
async def system_features() -> dict[str, bool]:
    """Optional capability flags consumed by upstream clients (the MCP
    server, the FE) to decide whether to surface features that depend
    on backend-side configuration.

    Currently advertised:

    * ``llm_classifier`` — is a real LLM provider configured? When
      ``False`` the propose / apply care-phase endpoints would always
      return 503 because ``get_llm_provider`` falls through to
      ``StubLLM``. The MCP server reads this flag at first
      ``list_tools()`` call to decide whether to surface
      ``propose_care_phases`` / ``apply_phase_proposal``: in BYO mode
      (no key) the agent classifies in its own LLM and uses
      ``create_care_phase`` + ``assign_event_to_phase`` directly.
    """
    return {
        "llm_classifier": (
            settings.llm_provider in ("anthropic", "auto") and bool(settings.anthropic_api_key)
        ),
    }


@app.get("/", tags=["infra"])
async def root() -> dict[str, str]:
    return {
        "name": "bitvision phoenix",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/.well-known/agent-card.json", tags=["a2a"])
async def agent_card() -> JSONResponse:
    """A2A Agent Card for discovery by external agents."""
    return JSONResponse(content=AGENT_CARD)
