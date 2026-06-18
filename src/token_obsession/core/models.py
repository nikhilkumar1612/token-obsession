"""Core domain models for the scoring engine."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Chain(StrEnum):
    """Supported chains."""

    BASE = "base"


class Strategy(StrEnum):
    """Initial ranking strategies."""

    FRESH_QUALITY = "fresh_quality"
    SAFER_MOMENTUM = "safer_momentum"
    HIGH_GREED_HIGH_RISK = "high_greed_high_risk"


class TokenSnapshot(BaseModel):
    """Canonical snapshot used by the scoring layer."""

    chain: Chain = Chain.BASE
    data_source: str = "bootstrap"
    token_address: str
    pool_address: str
    symbol: str
    name: str
    first_seen_at: datetime
    liquidity_usd: float = Field(ge=0)
    volume_15m_usd: float = Field(ge=0)
    volume_1h_usd: float = Field(ge=0)
    volume_acceleration: float = Field(ge=0)
    buy_count_15m: int = Field(ge=0)
    sell_count_15m: int = Field(ge=0)
    holder_growth_1h: int
    net_buyer_ratio: float = Field(ge=-1, le=1)
    risk_flags: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_count: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def age_minutes(self) -> int:
        """Return age in whole minutes."""

        age = datetime.now(UTC) - self.first_seen_at.astimezone(UTC)
        return max(int(age.total_seconds() // 60), 0)


class Opportunity(BaseModel):
    """Ranked token opportunity returned to agents."""

    chain: Chain
    strategy: Strategy
    data_source: str
    token_address: str
    pool_address: str
    symbol: str
    name: str
    age_minutes: int
    liquidity_usd: float
    volume_15m_usd: float
    volume_1h_usd: float
    volume_acceleration: float
    opportunity_score: float = Field(ge=0, le=10)
    risk_score: float = Field(ge=0, le=10)
    confidence_score: float = Field(ge=0, le=10)
    reasons: list[str]
    warnings: list[str]
    source_count: int
    updated_at: datetime


class ExplainTokenResult(BaseModel):
    """Detailed explanation for a single token."""

    opportunity: Opportunity
    risk_flags: list[str]
    summary: str


class CompareTokensResult(BaseModel):
    """Comparison payload for several token candidates."""

    strategy: Strategy
    tokens: list[Opportunity]
