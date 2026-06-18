from token_obsession.core.config import Settings
from token_obsession.core.models import Strategy
from token_obsession.services.scoring import BootstrapScoringService


def test_fresh_quality_only_returns_tokens_inside_window() -> None:
    service = BootstrapScoringService(settings=Settings())

    ranked = service.scan_tokens(strategy=Strategy.FRESH_QUALITY, limit=10)

    assert ranked
    assert all(token.age_minutes <= 360 for token in ranked)


def test_compare_tokens_orders_by_opportunity_score() -> None:
    service = BootstrapScoringService(settings=Settings())

    result = service.compare_tokens(
        token_addresses=[
            "0x4444444444444444444444444444444444444444",
            "0x1111111111111111111111111111111111111111",
        ],
        strategy=Strategy.HIGH_GREED_HIGH_RISK,
    )

    assert len(result.tokens) == 2
    assert result.tokens[0].opportunity_score >= result.tokens[1].opportunity_score
