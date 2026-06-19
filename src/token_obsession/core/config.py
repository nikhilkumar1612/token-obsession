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
    coingecko_api_key: str | None = None
    coingecko_base_url: str = "https://pro-api.coingecko.com/api/v3"
    coingecko_timeout_seconds: float = Field(default=10.0, gt=0)
    coingecko_max_pages: int = Field(default=2, ge=1, le=10)
    dexscreener_base_url: str = "https://api.dexscreener.com"
    dexscreener_timeout_seconds: float = Field(default=10.0, gt=0)
    birdeye_api_key: str | None = None
    birdeye_base_url: str = "https://public-api.birdeye.so"
    birdeye_timeout_seconds: float = Field(default=10.0, gt=0)
    birdeye_max_trending_tokens: int = Field(default=20, ge=1, le=20)

    model_config = SettingsConfigDict(
        env_prefix="TOKEN_OBSESSION_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings."""

    return Settings()
