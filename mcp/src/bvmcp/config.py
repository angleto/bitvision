"""MCP server settings."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BVP_MCP_",
        env_file=(".env", "../.env"),
        extra="ignore",
    )

    backend_base_url: str = Field(default="http://localhost:8000")
    # User-scoped token used by the MCP server to call the backend.
    # In production the MCP server is launched per-user with that user's
    # token; in dev this may be left empty.
    user_token: str = Field(default="")
    # Agent-scoped, short-lived token restricted to a specific patient.
    # When set, takes precedence over user_token. The backend distinguishes
    # user vs agent tokens via the JWT `typ` claim; MCP does not decode.
    agent_token: str | None = Field(default=None)


@lru_cache
def get_settings() -> Settings:
    return Settings()
