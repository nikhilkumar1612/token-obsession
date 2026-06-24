"""Configurable sell evaluation for manually tracked positions."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from token_obsession.core.config import Settings
from token_obsession.core.models import (
    Position,
    PositionSellEvaluation,
    PositionStatus,
    SellEvaluationConfig,
    SellEvaluationReport,
    SellRecommendation,
    VolumeComparisonWindow,
)
from token_obsession.services.dexscreener import (
    DexScreenerClient,
    DexScreenerClientError,
)

logger = logging.getLogger(__name__)


class SellEvaluationService:
    """Evaluate all open positions against live DEX Screener signals."""

    _volume_windows = {
        VolumeComparisonWindow.M5_VS_H1: ("m5", 5, "h1", 60),
        VolumeComparisonWindow.H1_VS_H6: ("h1", 60, "h6", 360),
        VolumeComparisonWindow.H6_VS_H24: ("h6", 360, "h24", 1440),
    }

    def __init__(
        self,
        settings: Settings,
        dex_client: DexScreenerClient | None = None,
    ) -> None:
        self._settings = settings
        self._dex_client = dex_client or DexScreenerClient(settings=settings)

    def evaluate_open_positions(
        self,
        positions: list[Position],
        config: SellEvaluationConfig,
    ) -> SellEvaluationReport:
        """Return a recommendation for every open position using one batched lookup."""

        open_positions = [
            position for position in positions if position.status == PositionStatus.OPEN
        ]
        if not open_positions:
            logger.info("SELL EVALUATION SKIPPED | reason=no_open_positions")
            return SellEvaluationReport(config=config, positions=[])

        token_addresses = [position.token_address for position in open_positions]
        logger.info(
            "DEX SCREENER LOOKUP STARTED | token_count=%d | tokens=%s",
            len(open_positions),
            ",".join(position.symbol for position in open_positions),
        )
        try:
            pairs = self._dex_client.get_token_pairs("base", token_addresses)
        except DexScreenerClientError as exc:
            logger.error(
                "DEX SCREENER LOOKUP FAILED | token_count=%d | error=%s",
                len(open_positions),
                exc,
            )
            evaluations = [
                self._no_data_evaluation(
                    position=position,
                    config=config,
                    warning="DEX Screener request failed; no recommendation was generated.",
                )
                for position in open_positions
            ]
            return SellEvaluationReport(config=config, positions=evaluations)

        logger.info("DEX SCREENER LOOKUP COMPLETED | pair_count=%d", len(pairs))
        best_pairs = self._best_pairs_by_token(pairs)
        matched_token_count = sum(
            token_address.lower() in best_pairs for token_address in token_addresses
        )
        logger.info(
            "PAIR SELECTION COMPLETED | matched_tokens=%d | missing_tokens=%d",
            matched_token_count,
            len(open_positions) - matched_token_count,
        )
        evaluations = [
            self._evaluate_position(
                position=position,
                pair=best_pairs.get(position.token_address.lower()),
                config=config,
            )
            for position in open_positions
        ]
        return SellEvaluationReport(config=config, positions=evaluations)

    def _evaluate_position(
        self,
        position: Position,
        pair: dict[str, Any] | None,
        config: SellEvaluationConfig,
    ) -> PositionSellEvaluation:
        if pair is None:
            return self._no_data_evaluation(
                position=position,
                config=config,
                warning="No Base DEX Screener pair was found for this token.",
            )

        current_price = self._optional_float(pair.get("priceUsd"))
        if current_price is not None and current_price <= 0:
            current_price = None

        price_change = self._optional_float(
            pair.get("priceChange", {}).get(config.price_change_window.value),
        )
        volume_change = self._volume_pace_change(
            volume=pair.get("volume", {}),
            comparison_window=config.volume_comparison_window,
        )
        liquidity_usd = self._optional_float(pair.get("liquidity", {}).get("usd"))

        current_value: float | None = None
        unrealized_pnl_usd: float | None = None
        unrealized_pnl_percent: float | None = None
        if current_price is not None:
            current_value = round(position.quantity * current_price, 6)
            unrealized_pnl_usd = round(current_value - position.cost_basis_usd, 6)
            unrealized_pnl_percent = round(
                ((current_price - position.entry_price_usd) / position.entry_price_usd) * 100,
                4,
            )

        hard_exit_triggers: list[str] = []
        market_exit_triggers: list[str] = []
        reasons: list[str] = []
        warnings: list[str] = []

        if unrealized_pnl_percent is None:
            warnings.append("Current price is unavailable, so PnL rules were skipped.")
        else:
            reasons.append(
                f"Unrealized PnL is {unrealized_pnl_percent:+.2f}% (${unrealized_pnl_usd:+,.2f}).",
            )
            if (
                config.take_profit_percent is not None
                and unrealized_pnl_percent >= config.take_profit_percent
            ):
                hard_exit_triggers.append("take_profit")
            if (
                config.stop_loss_percent is not None
                and unrealized_pnl_percent <= -config.stop_loss_percent
            ):
                hard_exit_triggers.append("stop_loss")

        if price_change is None:
            if config.price_drop_percent is not None:
                warnings.append(
                    f"{config.price_change_window.value} price change is unavailable.",
                )
        else:
            reasons.append(
                f"{config.price_change_window.value} price change is {price_change:+.2f}%.",
            )
            if config.price_drop_percent is not None and price_change <= -config.price_drop_percent:
                market_exit_triggers.append("price_drop")

        if volume_change is None:
            if config.volume_drop_percent is not None:
                warnings.append(
                    "Comparable rolling volume is unavailable for the selected window.",
                )
        else:
            reasons.append(
                f"Volume pace changed {volume_change:+.2f}% over "
                f"{config.volume_comparison_window.value}.",
            )
            if (
                config.volume_drop_percent is not None
                and volume_change <= -config.volume_drop_percent
            ):
                market_exit_triggers.append("volume_drop")

        recommendation = self._recommendation(
            hard_exit_triggers=hard_exit_triggers,
            market_exit_triggers=market_exit_triggers,
            minimum_market_signals=config.minimum_market_signals_for_sell,
        )
        return PositionSellEvaluation(
            position_id=position.position_id,
            chain=position.chain,
            token_address=position.token_address,
            symbol=position.symbol,
            quantity=position.quantity,
            entry_price_usd=position.entry_price_usd,
            current_price_usd=current_price,
            current_value_usd=current_value,
            unrealized_pnl_usd=unrealized_pnl_usd,
            unrealized_pnl_percent=unrealized_pnl_percent,
            price_change_window=config.price_change_window,
            price_change_percent=price_change,
            volume_comparison_window=config.volume_comparison_window,
            volume_change_percent=volume_change,
            liquidity_usd=liquidity_usd,
            recommendation=recommendation,
            hard_exit_triggers=hard_exit_triggers,
            market_exit_triggers=market_exit_triggers,
            reasons=reasons,
            warnings=warnings,
            data_source="dexscreener_token_pairs",
        )

    def _no_data_evaluation(
        self,
        position: Position,
        config: SellEvaluationConfig,
        warning: str,
    ) -> PositionSellEvaluation:
        return PositionSellEvaluation(
            position_id=position.position_id,
            chain=position.chain,
            token_address=position.token_address,
            symbol=position.symbol,
            quantity=position.quantity,
            entry_price_usd=position.entry_price_usd,
            price_change_window=config.price_change_window,
            volume_comparison_window=config.volume_comparison_window,
            recommendation=SellRecommendation.NO_DATA,
            warnings=[warning],
            data_source="dexscreener_token_pairs",
            evaluated_at=datetime.now(UTC),
        )

    def _best_pairs_by_token(
        self,
        pairs: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        best_pairs: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            if str(pair.get("chainId") or "").lower() != "base":
                continue
            base_token = pair.get("baseToken", {})
            token_address = str(base_token.get("address") or "").lower()
            if not token_address:
                continue
            existing = best_pairs.get(token_address)
            if existing is None or self._pair_rank_key(pair) > self._pair_rank_key(existing):
                best_pairs[token_address] = pair
        return best_pairs

    def _pair_rank_key(self, pair: dict[str, Any]) -> tuple[int, float, float]:
        quote_symbol = str(pair.get("quoteToken", {}).get("symbol") or "").upper()
        core_quote = quote_symbol in {"WETH", "ETH", "USDC", "USDBC", "DAI"}
        liquidity = self._optional_float(pair.get("liquidity", {}).get("usd")) or 0.0
        volume_h1 = self._optional_float(pair.get("volume", {}).get("h1")) or 0.0
        return (int(core_quote), liquidity, volume_h1)

    def _volume_pace_change(
        self,
        volume: dict[str, Any],
        comparison_window: VolumeComparisonWindow,
    ) -> float | None:
        short_key, short_minutes, long_key, long_minutes = self._volume_windows[comparison_window]
        short_volume = self._optional_float(volume.get(short_key))
        long_volume = self._optional_float(volume.get(long_key))
        if short_volume is None or long_volume is None:
            return None

        previous_volume = long_volume - short_volume
        previous_minutes = long_minutes - short_minutes
        if short_volume < 0 or previous_volume <= 0:
            return None

        recent_rate = short_volume / short_minutes
        previous_rate = previous_volume / previous_minutes
        return round(((recent_rate - previous_rate) / previous_rate) * 100, 4)

    @staticmethod
    def _recommendation(
        hard_exit_triggers: list[str],
        market_exit_triggers: list[str],
        minimum_market_signals: int,
    ) -> SellRecommendation:
        if hard_exit_triggers:
            return SellRecommendation.SELL
        if len(market_exit_triggers) >= minimum_market_signals:
            return SellRecommendation.SELL
        if market_exit_triggers:
            return SellRecommendation.WATCH
        return SellRecommendation.HOLD

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
