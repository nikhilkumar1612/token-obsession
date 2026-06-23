"""Application configuration."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from token_obsession.core.models import PriceChangeWindow, VolumeComparisonWindow


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
    positions_file_path: Path = Path(".token_obsession/positions.json")
    sell_proposals_file_path: Path = Path(".token_obsession/sell_proposals.json")
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
    sell_check_interval_minutes: int = Field(default=30, ge=1)
    sell_take_profit_percent: float | None = Field(default=30.0, gt=0)
    sell_stop_loss_percent: float | None = Field(default=15.0, gt=0)
    sell_price_drop_percent: float | None = Field(default=10.0, gt=0)
    sell_price_change_window: PriceChangeWindow = PriceChangeWindow.H1
    sell_volume_drop_percent: float | None = Field(default=50.0, gt=0)
    sell_volume_comparison_window: VolumeComparisonWindow = VolumeComparisonWindow.M5_VS_H1
    sell_minimum_market_signals: int = Field(default=2, ge=1, le=2)
    sell_percentage: float = Field(default=100.0, gt=0, le=100)
    sell_slippage_percent: float = Field(default=2.5, gt=0, le=100)
    sell_reproposal_cooldown_minutes: int = Field(default=360, ge=0)
    sell_proposals_enabled: bool = False
    base_chain_id: int = 8453
    base_rpc_url: str | None = None
    base_usdc_address: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    safe_address: str | None = None
    safe_api_key: SecretStr | None = None
    safe_delegate_private_key: SecretStr | None = None
    safe_request_timeout_seconds: int = Field(default=10, gt=0)
    uniswap_api_key: SecretStr | None = None
    uniswap_base_url: str = "https://trade-api.gateway.uniswap.org/v1"
    uniswap_timeout_seconds: float = Field(default=30.0, gt=0)
    uniswap_swap_deadline_minutes: int = Field(default=1440, ge=1)

    model_config = SettingsConfigDict(
        env_prefix="TOKEN_OBSESSION_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings."""

    return Settings()
