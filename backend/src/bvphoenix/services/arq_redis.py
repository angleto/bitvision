"""arq RedisSettings constructor that honours the ssl_cert_reqs query param.

``arq.connections.RedisSettings.from_dsn`` parses host / port / password /
database / scheme out of the URL and ignores the rest. A
``rediss://...?ssl_cert_reqs=none`` URL is therefore parsed but the cert
verification stays at the default ``required``, which fails against a
self-signed TLS cert (Scaleway Managed Redis ships one).

This helper post-processes the dataclass: if the URL is ``rediss://``
and the query string asks for a non-default ``ssl_cert_reqs``, propagate
it onto the settings. Mirrors what redis-py's ``Redis.from_url`` already
does.

Note arq declares ``ssl_cert_reqs: str`` (one of ``'none'`` /
``'optional'`` / ``'required'``); the integer enum from the ``ssl``
module breaks redis-py's RedisSSLContext on Python 3.12+.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from arq.connections import RedisSettings


def redis_settings(dsn: str) -> RedisSettings:
    """Build :class:`RedisSettings` from a DSN, propagating ssl_cert_reqs."""
    rs = RedisSettings.from_dsn(dsn)
    if rs.ssl:
        q = parse_qs(urlparse(dsn).query)
        v = (q.get("ssl_cert_reqs") or [""])[0].lower()
        if v in ("none", "optional", "required"):
            rs.ssl_cert_reqs = v
    return rs
