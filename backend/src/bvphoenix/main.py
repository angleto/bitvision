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
    # The built-in docs live outside ``/api`` and so are shadowed by the
    # frontend catch-all in production (the ingress only routes ``/api/*``
    # to the backend) and are unauthenticated. We disable all three
    # defaults and re-serve them, auth-gated, under ``/api`` — see
    # ``bvphoenix.api.docs``.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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


@app.on_event("startup")
async def _probe_pgvector_capabilities() -> None:
    """Detect the connected pgvector version once, so the vector search
    paths know whether they may use ``hnsw.iterative_scan`` (0.8+).

    Failure is non-fatal: the capability defaults to ``False`` (the
    0.8-only GUC is simply never emitted), so a probe error degrades to
    plain HNSW rather than 500-ing the whole app at boot."""
    import logging

    from sqlalchemy import text

    from bvphoenix.db.session import SessionFactory
    from bvphoenix.services.vector_search import (
        ITERATIVE_SCAN_MIN_VERSION,
        parse_pgvector_version,
        set_iterative_scan_supported,
    )

    log = logging.getLogger("bvphoenix.startup")
    try:
        async with SessionFactory() as db:
            raw = (
                await db.execute(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
            ).scalar_one_or_none()
        version = parse_pgvector_version(raw)
        supported = version is not None and version >= ITERATIVE_SCAN_MIN_VERSION
        set_iterative_scan_supported(supported)
        log.info(
            "pgvector %s detected; hnsw.iterative_scan %s",
            raw or "unknown",
            "enabled" if supported else "unavailable (need >= 0.8)",
        )
    except Exception as exc:  # pragma: no cover — defensive boot path
        log.warning("pgvector capability probe skipped (%s); iterative_scan disabled", exc)


@app.on_event("startup")
async def _check_embedding_registry_defaults() -> None:
    """Resolve the registry's default image/text models at boot, cache
    their ids on ``app.state``, and warn loudly if a default diverges from
    the ``model_id`` search actually queries.

    The registry (``embedding_models``) is meant to be the source of
    truth for which model search uses; migration 0011 reconciled its seed
    with what the workers write. This guard catches future drift (e.g. an
    operator promoting a new default whose name does not match a stored
    ``model_id``) before it silently empties search results. Non-fatal:
    the endpoints fall back to their validated constants."""
    import logging

    from bvphoenix.db.session import SessionFactory
    from bvphoenix.services.chunk_search import MULTILINGUAL_MODEL_ID
    from bvphoenix.services.embedding_models import get_default_model

    log = logging.getLogger("bvphoenix.startup")
    # The model_id strings the code/workers actually read and write.
    expected = {"image": "biomedclip-v1", "text": MULTILINGUAL_MODEL_ID}
    try:
        async with SessionFactory() as db:
            for kind, expected_id in expected.items():
                model = await get_default_model(kind, db)
                setattr(app.state, f"default_{kind}_model_id", model.name)
                if model.name != expected_id:
                    log.warning(
                        "embedding registry default for %r is %r but search queries %r — "
                        "reconcile the registry or the code, or %r search will return empty",
                        kind,
                        model.name,
                        expected_id,
                        kind,
                    )
    except Exception as exc:  # pragma: no cover — defensive boot path
        log.warning("embedding registry default check skipped: %s", exc)


@app.on_event("startup")
async def _load_search_thesaurus() -> None:
    """Warm the radiology synonym cache so query expansion is a synchronous
    lookup with no per-search DB round-trip. Non-fatal: an empty cache just
    degrades to plain dual-config FTS."""
    import logging

    from bvphoenix.db.session import SessionFactory
    from bvphoenix.services.thesaurus import load_thesaurus, thesaurus_version

    log = logging.getLogger("bvphoenix.startup")
    try:
        async with SessionFactory() as db:
            n = await load_thesaurus(db)
        log.info("search thesaurus loaded: %d terms (version %d)", n, thesaurus_version())
    except Exception as exc:  # pragma: no cover — defensive boot path
        log.warning("search thesaurus load skipped: %s", exc)


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
    # Custom response headers the viewer reads from JS. The browser hides
    # non-safelisted response headers cross-origin unless they are listed
    # here. ``X-Volume-*`` carry the packed volume's real patient-space
    # geometry; ``X-Slice-*`` / ``X-Document-*`` drive the 2D slice viewer
    # default slice and document kind/title.
    expose_headers=[
        "X-Volume-Origin",
        "X-Volume-Direction",
        "X-Volume-Frame-Of-Reference",
        "X-Slice-Index",
        "X-Slice-Count",
        "X-Document-Kind",
        "X-Document-Title",
    ],
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
        "docs": "/api/docs",
        "health": "/health",
    }


@app.get("/.well-known/agent-card.json", tags=["a2a"])
async def agent_card() -> JSONResponse:
    """A2A Agent Card for discovery by external agents."""
    return JSONResponse(content=AGENT_CARD)
