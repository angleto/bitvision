"""Authentication & token issuance.

bitvision phoenix uses local username + bcrypt + JWT signed with
``BVP_JWT_SECRET``. AI assistants authenticate to the MCP transport
with per-assistant client_id/client_secret pairs (see
``api/ai_assistants.py``); the MCP gate resolves the bearer secret
hash via ``api/internal_auth.py``.
"""

from bvphoenix.auth.deps import (
    active_share_grant,
    bearer_scheme,
    enforce_agent_patient_scope,
    enforce_agent_scope,
    optional_user,
    require_admin,
    require_agent_scope,
    require_scope_if_agent,
    require_user,
)
from bvphoenix.auth.passwords import hash_password, verify_password
from bvphoenix.auth.tokens import decode_token, issue_access_token, issue_agent_token

__all__ = [
    "active_share_grant",
    "bearer_scheme",
    "decode_token",
    "enforce_agent_patient_scope",
    "enforce_agent_scope",
    "hash_password",
    "issue_access_token",
    "issue_agent_token",
    "optional_user",
    "require_admin",
    "require_agent_scope",
    "require_scope_if_agent",
    "require_user",
    "verify_password",
]
