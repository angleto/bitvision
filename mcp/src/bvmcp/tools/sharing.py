"""MCP tools — share-link CRUD.

Sharing is NOT human-only by design: the backend
(``backend/src/bvphoenix/api/sharing.py``) accepts agent tokens and
already enforces the right invariants for them. ``enforce_agent_patient_scope``
runs before the owner check so a leaked token cannot enumerate
cross-patient ownership via 403-vs-404; the owner check
``user.subject_id == grant.grantor_subject_id || user.is_admin`` then
gates the mutation. The de-identify default (``resolve_deidentify_default``)
turns PHI scrub ON for external grantees automatically.

This module exposes share-link writes to the agent under a single
sensitive scope ``sharing:write``: the operator must opt in
explicitly because mintaging a share-link delegates clinical access to
the outside. The scope is sensitive in the same sense as
``qna:ask`` / ``lookup:external``.

Surface (5 tools, narrower than the backend on purpose):

* ``create_study_share_link`` / ``create_folder_share_link`` — mint a
  share. Supports ``dry_run=true`` for preview without commit
  (backend extension landed alongside this module).
* ``list_share_links`` — flat listing or per-study scope so an agent
  can avoid duplicating an existing share before creating a new one.
* ``update_share_link`` — extend validity, rotate password, change
  access level. Forbidden on revoked links (the backend 409s).
* ``revoke_share_link`` — soft-revoke; pass ``purge=true`` to hard
  delete a previously-revoked link.

Out of scope (deliberately):

* ``/shared/{token}/*`` endpoints — those are the *recipient*'s API,
  invoked by the unauthenticated landing page. Nothing for the
  agent there.
* ``/share-links/{token}/claim`` — used when a grantee accepts the
  link from their browser. Not an agent operation.
* Publish / unpublish on a study (``/studies/{id}/publish``) — opt-in
  flag for the curated public catalog; out of scope of this round.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.types import Tool

from bvmcp.tools.client import (
    api_delete,
    api_get,
    api_patch,
    api_post_with_headers,
    format_http_error,
)

_HINT = (
    "share-link blast radius is high: mintaging exposes patient data "
    "to the outside. Pass dry_run=true first to preview RBAC + "
    "grantee resolution + deidentify default without committing."
)


# ---------------------------------------------------------------------
# Shared body shape — keep the schema close to the backend ShareCreateIn
# so the JSON the agent assembles round-trips verbatim.
# ---------------------------------------------------------------------
_SHARE_TARGET_SCHEMA = {
    "type": "object",
    "description": (
        "Who the share is for. ``link_public`` mints a tokened URL "
        "anyone can open (with optional password); ``link_org`` "
        "narrows the link to grantees inside ``org_subject_id``; "
        "``email`` mints a per-user grant resolved by email; ``org`` "
        "mints an org-wide grant without a public token."
    ),
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["link_public", "link_org", "email", "org"],
        },
        "email": {
            "type": "string",
            "description": ("Required when kind=email. Lower-cased server-side."),
        },
        "org_subject_id": {
            "type": "string",
            "description": ("Required when kind=org or kind=link_org. UUID of the org subject."),
        },
    },
    "required": ["kind"],
}


def _share_create_properties() -> dict[str, Any]:
    return {
        "access_level": {
            "type": "string",
            "enum": ["viewer", "editor", "manager"],
            "default": "viewer",
        },
        "download": {
            "type": "boolean",
            "default": False,
            "description": "Allow DICOM download. Off by default.",
        },
        "target": _SHARE_TARGET_SCHEMA,
        "expires_in_hours": {
            "type": "integer",
            "description": (
                "Validity window in hours. Default 168 (7 days). "
                "Pass null for a never-expiring link (discouraged "
                "for external grants)."
            ),
        },
        "password": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": (
                "Optional pre-shared password. Mutually exclusive with ``autogen_password``."
            ),
        },
        "autogen_password": {
            "type": "boolean",
            "default": False,
            "description": (
                "Server picks a 24-char high-entropy password and "
                "returns it ONCE in ``generated_password``. The "
                "plaintext is never stored; the caller must capture "
                "it on the response and deliver it out-of-band."
            ),
        },
        "label": {"type": "string", "maxLength": 255},
        "max_uses": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional usage cap.",
        },
        "mode": {
            "type": "string",
            "enum": ["claim", "anonymous"],
            "default": "claim",
            "description": (
                "``claim`` (default) requires the grantee to land on "
                "a magic-link account before reading; ``anonymous`` "
                "treats the link itself as the credential — every "
                "downstream write is attributed via "
                "ActorContext.kind='link'. Anonymous mode increases "
                "blast radius; prefer ``claim`` unless the recipient "
                "explicitly cannot create an account."
            ),
        },
        "recipient_name": {"type": "string", "maxLength": 255},
        "recipient_email": {"type": "string", "maxLength": 255},
        "recipient_phone": {"type": "string", "maxLength": 64},
        "deidentify": {
            "type": "boolean",
            "description": (
                "Strip PHI from served DICOMs. Default null = policy "
                "default (ON for external grants, OFF for internal). "
                "Set explicitly only to override the policy."
            ),
        },
        "ai_sponsorship_cap_cents": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Optional AI budget the grantor sponsors for the "
                "grantee. NULL = grantee pays from their own wallet."
            ),
        },
        "dry_run": {
            "type": "boolean",
            "default": False,
            "description": (
                "Validate-only mode: RBAC, agent-patient-scope, "
                "grantee resolution and deidentify default run; "
                "nothing is committed. The response carries "
                "placeholder ``id``/``token``/``url`` so it cannot "
                "be confused with a real share. Recommended before "
                "the first real mint, especially when the agent has "
                "uncertainty about the target email or org id."
            ),
        },
        "idempotency_key": {
            "type": "string",
            "maxLength": 255,
            "description": (
                "Idempotency-Key forwarded to the backend. Retry the "
                "same call with the same key inside the dedupe TTL "
                "(24h) and the backend returns the original share "
                "instead of minting a second one. Strongly "
                "recommended for any autonomous retry loop: without "
                "this header a network blip between the agent and "
                "the backend can mint two visible share-links for "
                "the same intent."
            ),
        },
    }


TOOLS: list[Tool] = [
    Tool(
        name="create_study_share_link",
        description=(
            "Mint a share-link (or direct grant) for a study. Owner "
            "or admin only — the backend rejects non-owners with 403, "
            "and an agent token must additionally hold the patient in "
            "its patient_ids scope. ``target.kind`` picks the audience "
            "(link_public / link_org / email / org). External grants "
            "ship with PHI scrub ON by default; override via "
            "``deidentify=false`` only when the clinical use case "
            "requires PHI. Pass ``dry_run=true`` to validate without "
            "committing. The response on a real mint carries "
            "``generated_password`` ONCE when ``autogen_password=true`` "
            "— capture it from the response, it is never returned "
            "again. See help(topic='agent_writes') for the wider "
            "audit + provenance contract."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {"type": "string", "description": "UUID of the study."},
                **_share_create_properties(),
            },
            "required": ["study_id", "target"],
        },
    ),
    Tool(
        name="create_folder_share_link",
        description=(
            "Mint a share-link for a folder. Cascades a Grant per "
            "first-class item inside the folder (studies, documents, "
            "etc.) so the grantee can reach the contents through the "
            "public ``/shared/<token>`` landing. Same rules as "
            "``create_study_share_link``: dry_run supported, "
            "autogen_password returns the secret once on the real "
            "mint, deidentify defaults follow the policy in "
            "authorization.md §7."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "folder_id": {"type": "string"},
                **_share_create_properties(),
            },
            "required": ["folder_id", "target"],
        },
    ),
    Tool(
        name="list_share_links",
        description=(
            "List share-links the caller owns. ``study_id``, when "
            "set, narrows to a single study via the dedicated "
            "endpoint; otherwise the response is the flat cross-"
            "patient listing (still filtered to the caller's "
            "grants). Use this before ``create_*_share_link`` to "
            "avoid duplicating an existing live share."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "study_id": {
                    "type": "string",
                    "description": (
                        "Optional study filter. When set, calls "
                        "GET /studies/{id}/shares; otherwise calls "
                        "GET /share-links."
                    ),
                },
                "patient_id": {
                    "type": "string",
                    "description": (
                        "Optional patient filter on the flat listing "
                        "(ignored when ``study_id`` is set)."
                    ),
                },
                "include_revoked": {"type": "boolean", "default": False},
                "include_expired": {"type": "boolean", "default": False},
                "limit": {
                    "type": "integer",
                    "default": 200,
                    "minimum": 1,
                    "maximum": 500,
                },
            },
        },
    ),
    Tool(
        name="update_share_link",
        description=(
            "Edit an existing share-link in place. Useful for: "
            "rotating the password (pass ``password``), extending "
            "validity (``expires_in_hours``), broadening / narrowing "
            "access (``access_level`` / ``download``), flipping "
            "deidentify. Forbidden on revoked links (the backend 409s — "
            "create a new share instead). Only the grantor or an "
            "admin can edit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "link_id": {"type": "string"},
                "label": {"type": "string", "maxLength": 255},
                "access_level": {
                    "type": "string",
                    "enum": ["viewer", "editor", "manager"],
                },
                "download": {"type": "boolean"},
                "expires_in_hours": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "0 = never expires.",
                },
                "max_uses": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "0 = unlimited.",
                },
                "password": {
                    "type": "string",
                    "minLength": 0,
                    "maxLength": 256,
                    "description": (
                        "Empty string clears the password; omit the field to leave it unchanged."
                    ),
                },
                "deidentify": {"type": "boolean"},
            },
            "required": ["link_id"],
        },
    ),
    Tool(
        name="revoke_share_link",
        description=(
            "Soft-revoke (default) or hard-delete (``purge=true``) a "
            "share-link. Soft revoke stamps ``revoked_at`` and keeps "
            "the row in the listing so the audit trail and use_count "
            "survive; purge hard-deletes the row + its grant and is "
            "only allowed on already-revoked links (the backend 409s "
            "if you skip the revoke step). The dedup primitive on "
            "the prep-job cache means the same cached archive may "
            "still serve a sibling live share — the backend only "
            "cancels the prep job when no other live share is using "
            "it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "link_id": {"type": "string"},
                "purge": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, hard-delete the row + grant. "
                        "Requires the link to be already revoked."
                    ),
                },
            },
            "required": ["link_id"],
        },
    ),
]


# ---------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------


def _share_body(args: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for k in (
        "access_level",
        "download",
        "target",
        "expires_in_hours",
        "password",
        "autogen_password",
        "label",
        "max_uses",
        "mode",
        "recipient_name",
        "recipient_email",
        "recipient_phone",
        "deidentify",
        "ai_sponsorship_cap_cents",
    ):
        if k in args:
            body[k] = args[k]
    return body


async def _create_study_share_link(args: dict[str, Any]) -> str:
    study_id = args["study_id"]
    body = _share_body(args)
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    idem_key = args.get("idempotency_key")
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/studies/{study_id}/share",
            json=body,
            params=params or None,
            idempotency_key=idem_key if isinstance(idem_key, str) and idem_key else None,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _create_folder_share_link(args: dict[str, Any]) -> str:
    folder_id = args["folder_id"]
    body = _share_body(args)
    params: dict[str, Any] = {}
    if args.get("dry_run"):
        params["dry_run"] = "true"
    idem_key = args.get("idempotency_key")
    try:
        payload, _headers = await api_post_with_headers(
            f"/api/folders/{folder_id}/share-link",
            json=body,
            params=params or None,
            idempotency_key=idem_key if isinstance(idem_key, str) and idem_key else None,
        )
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _list_share_links(args: dict[str, Any]) -> str:
    study_id = args.get("study_id")
    if study_id:
        # Study-scoped listing has a dedicated endpoint that walks
        # only the shares of that study; no extra params.
        payload = await api_get(f"/api/studies/{study_id}/shares")
        return json.dumps(payload, indent=2, ensure_ascii=False)
    params: dict[str, Any] = {}
    if args.get("patient_id"):
        params["patient_id"] = args["patient_id"]
    if args.get("include_revoked"):
        params["include_revoked"] = "true"
    if args.get("include_expired"):
        params["include_expired"] = "true"
    if args.get("limit") is not None:
        params["limit"] = int(args["limit"])
    payload = await api_get("/api/share-links", params=params or None)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _update_share_link(args: dict[str, Any]) -> str:
    link_id = args["link_id"]
    body: dict[str, Any] = {}
    for k in (
        "label",
        "access_level",
        "download",
        "expires_in_hours",
        "max_uses",
        "password",
        "deidentify",
    ):
        if k in args:
            body[k] = args[k]
    try:
        payload, _headers = await api_patch(f"/api/share-links/{link_id}", json=body)
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_HINT)
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _revoke_share_link(args: dict[str, Any]) -> str:
    link_id = args["link_id"]
    purge = bool(args.get("purge", False))
    path = f"/api/share-links/{link_id}"
    if purge:
        path += "?purge=true"
    try:
        code = await api_delete(path)
    except httpx.HTTPStatusError as exc:
        return format_http_error(exc, hint=_HINT)
    return json.dumps(
        {
            "status": "purged" if purge else "revoked",
            "link_id": link_id,
            "http_status": code,
        }
    )


_DISPATCH = {
    "create_study_share_link": _create_study_share_link,
    "create_folder_share_link": _create_folder_share_link,
    "list_share_links": _list_share_links,
    "update_share_link": _update_share_link,
    "revoke_share_link": _revoke_share_link,
}


async def handle(name: str, arguments: dict) -> str:
    fn = _DISPATCH.get(name)
    if fn is None:
        raise ValueError(f"unknown tool: {name}")
    return await fn(arguments or {})


__all__ = ["TOOLS", "handle"]
