from datetime import UTC, datetime

import pytest

from token_obsession.core.config import Settings
from token_obsession.core.models import (
    Position,
    SellEvaluationConfig,
    SellRecommendation,
)
from token_obsession.services.sell_evaluation import SellEvaluationService


class FakeDexClient:
    def __init__(self, pairs: list[dict]) -> None:
        self.pairs = pairs
        self.calls: list[tuple[str, list[str]]] = []

    def get_token_pairs(self, chain_id: str, token_addresses: list[str]) -> list[dict]:
        self.calls.append((chain_id, token_addresses))
        return self.pairs


def _position(
    token_address: str = "0xface",
    symbol: str = "FACE",
    entry_price_usd: float = 1.0,
) -> Position:
    return Position(
        position_id=f"position-{symbol.lower()}",
        token_address=token_address,
        symbol=symbol,
        quantity=100,
        entry_price_usd=entry_price_usd,
        entry_time=datetime(2026, 6, 22, tzinfo=UTC),
    )


def _pair(
    token_address: str = "0xface",
    symbol: str = "FACE",
    price_usd: float = 1.0,
    price_change_h1: float = 0.0,
    volume_m5: float = 100.0,
    volume_h1: float = 1200.0,
) -> dict:
    return {
        "chainId": "base",
        "baseToken": {"address": token_address, "symbol": symbol},
        "quoteToken": {"symbol": "WETH"},
        "priceUsd": str(price_usd),
        "priceChange": {"m5": 0, "h1": price_change_h1, "h6": 0, "h24": 0},
        "volume": {"m5": volume_m5, "h1": volume_h1, "h6": 7200, "h24": 28800},
        "liquidity": {"usd": 250000},
    }


@pytest.mark.parametrize(
    ("price_usd", "config", "expected_trigger"),
    [
        (1.25, SellEvaluationConfig(take_profit_percent=20), "take_profit"),
        (0.80, SellEvaluationConfig(stop_loss_percent=15), "stop_loss"),
    ],
)
def test_profit_and_loss_thresholds_are_hard_sell_signals(
    price_usd: float,
    config: SellEvaluationConfig,
    expected_trigger: str,
) -> None:
    dex_client = FakeDexClient([_pair(price_usd=price_usd)])
    service = SellEvaluationService(settings=Settings(), dex_client=dex_client)

    report = service.evaluate_open_positions([_position()], config=config)

    evaluation = report.positions[0]
    assert evaluation.recommendation == SellRecommendation.SELL
    assert evaluation.hard_exit_triggers == [expected_trigger]


def test_price_and_volume_declines_together_trigger_sell() -> None:
    dex_client = FakeDexClient(
        [
            _pair(
                price_usd=0.98,
                price_change_h1=-12,
                volume_m5=10,
                volume_h1=1010,
            )
        ]
    )
    service = SellEvaluationService(settings=Settings(), dex_client=dex_client)

    report = service.evaluate_open_positions(
        [_position()],
        config=SellEvaluationConfig(
            price_drop_percent=10,
            volume_drop_percent=50,
            minimum_market_signals_for_sell=2,
        ),
    )

    evaluation = report.positions[0]
    assert evaluation.recommendation == SellRecommendation.SELL
    assert evaluation.market_exit_triggers == ["price_drop", "volume_drop"]
    assert evaluation.volume_change_percent == pytest.approx(-89.0)


def test_market_thresholds_can_be_tuned_to_avoid_a_sell_signal() -> None:
    dex_client = FakeDexClient(
        [
            _pair(
                price_usd=0.98,
                price_change_h1=-12,
                volume_m5=10,
                volume_h1=1010,
            )
        ]
    )
    service = SellEvaluationService(settings=Settings(), dex_client=dex_client)

    report = service.evaluate_open_positions(
        [_position()],
        config=SellEvaluationConfig(
            price_drop_percent=20,
            volume_drop_percent=95,
        ),
    )

    evaluation = report.positions[0]
    assert evaluation.recommendation == SellRecommendation.HOLD
    assert evaluation.market_exit_triggers == []


def test_all_positions_are_fetched_in_one_dex_request() -> None:
    positions = [
        _position(),
        _position(token_address="0xbeef", symbol="BEEF", entry_price_usd=2.0),
    ]
    dex_client = FakeDexClient(
        [
            _pair(),
            _pair(token_address="0xbeef", symbol="BEEF", price_usd=2.0),
        ]
    )
    service = SellEvaluationService(settings=Settings(), dex_client=dex_client)

    report = service.evaluate_open_positions(
        positions,
        config=SellEvaluationConfig(),
    )

    assert len(report.positions) == 2
    assert dex_client.calls == [("base", ["0xface", "0xbeef"])]
