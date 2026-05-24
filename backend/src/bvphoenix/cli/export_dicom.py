"""`bvphoenix-export` — authenticated bulk download CLI.

Counterpart to ``bvphoenix-import``: resolves a query against the API,
follows the 307 presigned-URL redirects for every instance, and writes
``.dcm`` files into a local tree organised as
``<out>/<study_uid>/<series_uid>/<sop_uid>.dcm``.

Authentication: either ``--token`` (a JWT) or ``--email`` +
``--password`` (which calls ``/api/auth/login``).  Anonymous is also
accepted and will only see public studies — useful for scripted
replication of demo datasets.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import click


def _http_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    body: dict | None = None,
) -> dict | list:
    headers = {"accept": "application/json"}
    data: bytes | None = None
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _login(base_url: str, email: str, password: str) -> str:
    data = _http_json(
        f"{base_url}/api/auth/login",
        method="POST",
        body={"email": email, "password": password},
    )
    assert isinstance(data, dict)
    return data["access_token"]


def _download(url: str, *, token: str | None, out_path: Path) -> int:
    headers: dict[str, str] = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    # urllib follows 307 redirects by default via HTTPRedirectHandler.
    # But the redirect target is a presigned S3 URL that must NOT carry
    # the Authorization header (it has its own signature), so strip it
    # manually.
    opener = urllib.request.build_opener(_StripAuthOn307Handler())
    with opener.open(req) as resp:
        total = 0
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("wb") as fh:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
    return total


class _StripAuthOn307Handler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and "authorization" in {k.lower() for k in new_req.headers}:
            for k in list(new_req.headers):
                if k.lower() == "authorization":
                    del new_req.headers[k]
        return new_req


@click.command(
    name="bvphoenix-export",
    help="Download studies matching a query to a local folder.",
)
@click.option("--base-url", default="http://localhost:8000", show_default=True)
@click.option("--token", default=None, help="JWT (skip login).")
@click.option("--email", default=None)
@click.option("--password", default=None)
@click.option("--q", default=None, help="Full-text query.")
@click.option("--modality", default=None)
@click.option("--body-part", default=None)
@click.option("--limit", default=200, show_default=True, type=int)
@click.option(
    "--out",
    type=click.Path(path_type=Path, file_okay=False),
    required=True,
    help="Destination folder.",
)
def main(
    base_url: str,
    token: str | None,
    email: str | None,
    password: str | None,
    q: str | None,
    modality: str | None,
    body_part: str | None,
    limit: int,
    out: Path,
) -> None:
    if not token and email and password:
        token = _login(base_url, email, password)

    query: dict[str, str | int] = {"limit": limit}
    if q:
        query["q"] = q
    if modality:
        query["modality"] = modality
    if body_part:
        query["body_part"] = body_part
    qs = urllib.parse.urlencode(query)
    results = _http_json(f"{base_url}/api/search?{qs}", token=token)
    assert isinstance(results, dict)
    studies = results["items"]
    click.echo(
        f"{results['total']} matching stud{'y' if results['total'] == 1 else 'ies'}; "
        f"downloading first {len(studies)}"
    )

    total_bytes = 0
    for study in studies:
        detail = _http_json(f"{base_url}/api/studies/{study['id']}", token=token)
        assert isinstance(detail, dict)
        click.echo(
            f"→ {detail['study_description'] or detail['study_instance_uid']}"
            f" ({len(detail['series'])} series)"
        )
        for series in detail["series"]:
            instances = _http_json(f"{base_url}/api/series/{series['id']}/instances", token=token)
            assert isinstance(instances, list)
            for inst in instances:
                dest = (
                    out
                    / detail["study_instance_uid"]
                    / series["series_instance_uid"]
                    / f"{inst['sop_instance_uid']}.dcm"
                )
                if dest.exists():
                    continue
                size = _download(
                    f"{base_url}/api/instances/{inst['id']}/file",
                    token=token,
                    out_path=dest,
                )
                total_bytes += size

    click.echo(f"done — {total_bytes / 1_048_576:.1f} MiB written under {out}")
    if not studies:
        sys.exit(1)


if __name__ == "__main__":
    main()
