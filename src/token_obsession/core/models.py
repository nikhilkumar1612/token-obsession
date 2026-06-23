"""Core domain models for the scoring engine."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field, field_validator


class Chain(StrEnum):
    """Supported chains."""

    BASE = "base"


class Strategy(StrEnum):
    """Initial ranking strategies."""

    FRESH_QUALITY = "fresh_quality"
    SAFER_MOMENTUM = "safer_momentum"
    ESTABLISHED_TRENDING_24H = "established_trending_24h"
    HIGH_GREED_HIGH_RISK = "high_greed_high_risk"


class PositionStatus(StrEnum):
    """Lifecycle states for a tracked position."""

    OPEN = "open"
    CLOSED = "closed"


class SellRecommendation(StrEnum):
    """Recommended action for an open position."""

    SELL = "sell"
    WATCH = "watch"
    HOLD = "hold"
    NO_DATA = "no_data"


class SellProposalStatus(StrEnum):
    """Lifecycle states for a Safe sell proposal."""

    PENDING = "pending"
    EXECUTED = "executed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class SellWorkerResultStatus(StrEnum):
    """Outcome of one worker pass over a tracked position."""

    NO_ACTION = "no_action"
    PROPOSED = "proposed"
    ALREADY_PENDING = "already_pending"
    COOLDOWN = "cooldown"
    ERROR = "error"


class PriceChangeWindow(StrEnum):
    """DEX Screener windows available for price-change evaluation."""

    M5 = "m5"
    H1 = "h1"
    H6 = "h6"
    H24 = "h24"


class VolumeComparisonWindow(StrEnum):
    """Rolling volume windows used to compare recent trading pace."""

    M5_VS_H1 = "m5_vs_h1"
    H1_VS_H6 = "h1_vs_h6"
    H6_VS_H24 = "h6_vs_h24"


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
    price_usd: float | None = Field(default=None, ge=0)
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
    price_usd: float | None = Field(default=None, ge=0)
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


class PositionCreate(BaseModel):
    """Payload for recording a manually entered token position."""

    chain: Chain = Chain.BASE
    token_address: str
    symbol: str
    name: str | None = None
    quantity: float = Field(gt=0)
    entry_price_usd: float = Field(gt=0)
    entry_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    strategy: Strategy | None = None
    notes: str | None = None

    @field_validator("token_address")
    @classmethod
    def normalize_token_address(cls, value: str) -> str:
        """Normalize token addresses for consistent storage."""

        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("token_address cannot be empty")
        return normalized

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        """Normalize token symbols for consistent storage."""

        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol cannot be empty")
        return normalized

    @field_validator("name", "notes")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        """Strip empty optional text values down to null."""

        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class Position(PositionCreate):
    """Tracked token position stored for later monitoring."""

    position_id: str
    status: PositionStatus = PositionStatus.OPEN
    exit_price_usd: float | None = Field(default=None, ge=0)
    closed_at: datetime | None = None
    close_reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    @property
    def cost_basis_usd(self) -> float:
        """Return the original dollar cost of the position."""

        return round(self.quantity * self.entry_price_usd, 6)

    @computed_field
    @property
    def realized_pnl_percent(self) -> float | None:
        """Return realized PnL if the position has been closed with an exit price."""

        if self.exit_price_usd is None:
            return None
        return round(
            ((self.exit_price_usd - self.entry_price_usd) / self.entry_price_usd) * 100,
            4,
        )


class SellEvaluationConfig(BaseModel):
    """Configurable thresholds for evaluating whether positions should be sold."""

    take_profit_percent: float | None = Field(default=30.0, gt=0)
    stop_loss_percent: float | None = Field(default=15.0, gt=0)
    price_drop_percent: float | None = Field(default=10.0, gt=0)
    price_change_window: PriceChangeWindow = PriceChangeWindow.H1
    volume_drop_percent: float | None = Field(default=50.0, gt=0)
    volume_comparison_window: VolumeComparisonWindow = VolumeComparisonWindow.M5_VS_H1
    minimum_market_signals_for_sell: int = Field(default=2, ge=1, le=2)


class PositionSellEvaluation(BaseModel):
    """Sell recommendation and supporting market evidence for one position."""

    position_id: str
    chain: Chain
    token_address: str
    symbol: str
    quantity: float
    entry_price_usd: float
    current_price_usd: float | None = None
    current_value_usd: float | None = None
    unrealized_pnl_usd: float | None = None
    unrealized_pnl_percent: float | None = None
    price_change_window: PriceChangeWindow
    price_change_percent: float | None = None
    volume_comparison_window: VolumeComparisonWindow
    volume_change_percent: float | None = None
    liquidity_usd: float | None = None
    recommendation: SellRecommendation
    hard_exit_triggers: list[str] = Field(default_factory=list)
    market_exit_triggers: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    data_source: str
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SellEvaluationReport(BaseModel):
    """Sell evaluations for every currently open position."""

    config: SellEvaluationConfig
    positions: list[PositionSellEvaluation]


class PreparedSwapTransaction(BaseModel):
    """Validated transaction that must execute before the swap."""

    transaction_to: str
    transaction_data: str
    transaction_value: int = Field(default=0, ge=0)


class SwapQuote(BaseModel):
    """Normalized same-chain swap quote ready for Safe transaction building."""

    quote_id: str
    tool: str
    preparation_transactions: list[PreparedSwapTransaction] = Field(default_factory=list)
    transaction_to: str
    transaction_data: str
    transaction_value: int = Field(ge=0)
    from_amount: int = Field(gt=0)
    estimated_to_amount: int = Field(gt=0)
    minimum_to_amount: int = Field(gt=0)


class SellProposalRecord(BaseModel):
    """Persisted Safe proposal used for deduplication and reconciliation."""

    proposal_id: str
    position_id: str
    token_address: str
    symbol: str
    safe_address: str
    safe_tx_hash: str
    safe_nonce: int = Field(ge=0)
    quote_id: str
    from_amount: int = Field(gt=0)
    minimum_to_amount: int = Field(gt=0)
    output_token_address: str
    status: SellProposalStatus = SellProposalStatus.PENDING
    execution_tx_hash: str | None = None
    failure_reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SellWorkerPositionResult(BaseModel):
    """Worker action taken for one position during a monitoring pass."""

    position_id: str
    symbol: str
    recommendation: SellRecommendation
    status: SellWorkerResultStatus
    message: str
    safe_tx_hash: str | None = None


class SellWorkerRunReport(BaseModel):
    """Summary of one complete sell-worker monitoring pass."""

    started_at: datetime
    completed_at: datetime
    positions_checked: int = Field(ge=0)
    results: list[SellWorkerPositionResult]
