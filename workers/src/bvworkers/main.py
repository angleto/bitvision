"""Arq worker entry point.

Run with: `uv run arq bvworkers.main.WorkerSettings`
"""

from urllib.parse import parse_qs, urlparse

from arq.connections import RedisSettings
from arq.cron import cron

from bvworkers.config import get_settings
from bvworkers.tasks import registry

_settings = get_settings()


def _redis_settings(dsn: str) -> RedisSettings:
    """RedisSettings from URL, honouring ssl_cert_reqs query param.

    arq's ``RedisSettings.from_dsn`` ignores query params, so a
    ``rediss://...?ssl_cert_reqs=none`` URL is parsed but the cert
    verification stays strict. Scaleway Managed Redis ships a
    self-signed cert, which fails strict verification — match what
    redis-py's ``from_url`` does and propagate the param manually.
    The arq dataclass declares ``ssl_cert_reqs: str``, so the value
    must be a string ('none', 'optional', 'required'); the integer
    enum (``ssl.CERT_NONE``) breaks redis-py's RedisSSLContext on
    Python 3.12+.
    """
    rs = RedisSettings.from_dsn(dsn)
    if rs.ssl:
        q = parse_qs(urlparse(dsn).query)
        v = (q.get("ssl_cert_reqs") or [""])[0].lower()
        if v in ("none", "optional", "required"):
            rs.ssl_cert_reqs = v
    return rs


async def startup(ctx: dict) -> None:  # type: ignore[type-arg]
    """Per-worker startup — database pool, S3 client, model loading go here."""
    ctx["settings"] = _settings


async def shutdown(ctx: dict) -> None:  # type: ignore[type-arg]
    """Per-worker teardown."""
    return None


# Cron schedule for periodic maintenance tasks. Two responsibilities:
#
#   1. Drain the ``jobs`` table + S3 artifacts past ``expires_at``.
#      Per-row TTL is days; hourly would be enough on its own.
#   2. Reap stale ``running`` / ``queued`` rows whose worker died
#      mid-flight. The reaper window is 5 min (see
#      ``cleanup_jobs._STALE_AFTER``); firing the cron only hourly
#      would let a "ghost" in-progress job sit in the user's panel
#      for up to an hour after a rolling restart killed the runner.
#
# Compromise: every 5 minutes. The cleanup batch is bounded
# (``_BATCH_SIZE`` rows) and the reaper is a single statement-level
# UPDATE, so the job stays cheap; the wasted work in a quiet system
# is negligible.
CRON_JOBS = [
    cron(
        "bvworkers.tasks.cleanup_jobs.cleanup_expired_jobs",
        minute={2, 7, 12, 17, 22, 27, 32, 37, 42, 47, 52, 57},
        run_at_startup=False,
    ),
    # Sprint 3 (ADR 0006): hard-purge soft-deleted patient documents
    # past their retention window. Nightly at 03:13 to avoid the
    # cleanup_expired_jobs slot.
    cron(
        "bvworkers.tasks.purge_documents.purge_expired_documents",
        hour=3,
        minute=13,
        run_at_startup=False,
    ),
    # Sprint C (v3.5): outbound notification dispatcher. Scans
    # ``notification_dispatches`` for pending rows past their
    # scheduled time and ships them through the configured channel
    # (Scaleway TEM via SMTP for email, plus webhook backends when
    # enabled). Every 5 minutes — same cadence as cleanup_expired_jobs,
    # offset by one minute so the two crons don't lock the same DB
    # connection at exactly the same instant. The dispatcher itself
    # is idempotent; safe to retry on a worker restart.
    cron(
        "bvworkers.tasks.dispatch_notification.notification_safety_net",
        minute={3, 8, 13, 18, 23, 28, 33, 38, 43, 48, 53, 58},
        run_at_startup=False,
    ),
]


class WorkerSettings:
    functions = registry.FUNCTIONS
    cron_jobs = CRON_JOBS
    redis_settings = _redis_settings(_settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 60 * 30  # 30 min — DICOM ingestion can be long
    keep_result = 60 * 60
