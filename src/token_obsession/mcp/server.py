"""MCP server definition."""

from datetime import UTC, datetime

from mcp.server.fastmcp import FastMCP

from token_obsession.core.config import get_settings
from token_obsession.core.models import (
    Chain,
    CompareTokensResult,
    ExplainTokenResult,
    Opportunity,
    Position,
    PositionCreate,
    PriceChangeWindow,
    SellEvaluationConfig,
    SellEvaluationReport,
    Strategy,
    VolumeComparisonWindow,
)
from token_obsession.services.positions import PositionStore
from token_obsession.services.scoring import TokenScoringService
from token_obsession.services.sell_evaluation import SellEvaluationService

settings = get_settings()
scoring_service = TokenScoringService(settings=settings)
position_store = PositionStore(settings=settings)
sell_evaluation_service = SellEvaluationService(settings=settings)

mcp = FastMCP(
    name="token-obsession",
    instructions=(
        "Use this server to discover Base token opportunities, track manual positions, "
        "and evaluate open positions for sell signals. Treat all outputs as decision "
        "support, not guaranteed outcomes or executed trades."
    ),
    json_response=True,
    streamable_http_path="/",
)


@mcp.tool()
def scan_tokens(
    strategy: Strategy,
    limit: int = 5,
    chain: Chain = Chain.BASE,
) -> list[Opportunity]:
    """Return ranked Base token opportunities for one strategy."""

    return scoring_service.scan_tokens(strategy=strategy, limit=limit, chain=chain)


@mcp.tool()
def explain_token(
    token_address: str,
    strategy: Strategy = Strategy.FRESH_QUALITY,
) -> ExplainTokenResult:
    """Explain why a Base token ranks the way it does."""

    return scoring_service.explain_token(token_address=token_address, strategy=strategy)


@mcp.tool()
def compare_tokens(
    token_addresses: list[str],
    strategy: Strategy = Strategy.SAFER_MOMENTUM,
) -> CompareTokensResult:
    """Compare several Base token candidates under one strategy."""

    return scoring_service.compare_tokens(
        token_addresses=token_addresses,
        strategy=strategy,
    )


@mcp.tool()
def add_position(
    token_address: str,
    symbol: str,
    quantity: float,
    entry_price_usd: float,
    chain: Chain = Chain.BASE,
    entry_time: datetime | None = None,
    name: str | None = None,
    strategy: Strategy | None = None,
    notes: str | None = None,
) -> Position:
    """Store a manually bought token position for later monitoring."""

    position_payload = {
        "chain": chain,
        "token_address": token_address,
        "symbol": symbol,
        "name": name,
        "quantity": quantity,
        "entry_price_usd": entry_price_usd,
        "strategy": strategy,
        "notes": notes,
    }
    if entry_time is not None:
        position_payload["entry_time"] = entry_time
    else:
        position_payload["entry_time"] = datetime.now(UTC)

    return position_store.add_position(PositionCreate(**position_payload))


@mcp.tool()
def list_positions(include_closed: bool = False) -> list[Position]:
    """List tracked positions stored on the MCP server."""

    return position_store.list_positions(include_closed=include_closed)


@mcp.tool()
def close_position(
    position_id: str,
    exit_price_usd: float | None = None,
    closed_at: datetime | None = None,
    close_reason: str | None = None,
) -> Position:
    """Mark a tracked position as closed after a manual sell."""

    return position_store.close_position(
        position_id=position_id,
        exit_price_usd=exit_price_usd,
        closed_at=closed_at,
        close_reason=close_reason,
    )


@mcp.tool()
def evaluate_positions_for_sell(
    take_profit_percent: float | None = 30.0,
    stop_loss_percent: float | None = 15.0,
    price_drop_percent: float | None = 10.0,
    price_change_window: PriceChangeWindow = PriceChangeWindow.H1,
    volume_drop_percent: float | None = 50.0,
    volume_comparison_window: VolumeComparisonWindow = (VolumeComparisonWindow.M5_VS_H1),
    minimum_market_signals_for_sell: int = 2,
) -> SellEvaluationReport:
    """Evaluate every open position for sell signals without executing a trade.

    Pass null for an optional percentage threshold to disable that rule.
    """

    config = SellEvaluationConfig(
        take_profit_percent=take_profit_percent,
        stop_loss_percent=stop_loss_percent,
        price_drop_percent=price_drop_percent,
        price_change_window=price_change_window,
        volume_drop_percent=volume_drop_percent,
        volume_comparison_window=volume_comparison_window,
        minimum_market_signals_for_sell=minimum_market_signals_for_sell,
    )
    return sell_evaluation_service.evaluate_open_positions(
        positions=position_store.list_positions(),
        config=config,
    )
