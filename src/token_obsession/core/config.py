"""Application configuration."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the MCP host."""

    app_name: str = "token-obsession"
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    mcp_mount_path: str = "/mcp"
    default_chain: str = "base"
    fresh_window_hours: int = 6
    min_liquidity_usd: float = Field(default=15000.0, ge=0)

    model_config = SettingsConfigDict(
        env_prefix="TOKEN_OBSESSION_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings."""

    return Settings()
