from datetime import UTC, datetime, timedelta

from token_obsession.core.config import Settings
from token_obsession.core.models import Strategy
from token_obsession.services.scoring import TokenScoringService


def _fake_new_pools_response() -> dict:
    created_at = (
        datetime.now(UTC) - timedelta(minutes=25)
    ).isoformat().replace('+00:00', 'Z')
    return {
        'data': [
            {
                'id': 'base_0xpool1',
                'type': 'pool',
                'attributes': {
                    'address': '0xpool1',
                    'pool_created_at': created_at,
                    'reserve_in_usd': '95000',
                    'volume_usd': {
                        'm15': '21000',
                        'h1': '48000',
                    },
                    'transactions': {
                        'm15': {
                            'buys': 45,
                            'sells': 18,
                            'buyers': 39,
                            'sellers': 17,
                        },
                        'h1': {
                            'buyers': 110,
                            'sellers': 54,
                        },
                    },
                    'community_sus_report': 0,
                },
                'relationships': {
                    'base_token': {'data': {'id': 'base_token_1'}},
                    'quote_token': {'data': {'id': 'base_quote_1'}},
                },
            }
        ],
        'included': [
            {
                'id': 'base_token_1',
                'type': 'token',
                'attributes': {
                    'address': '0xface',
                    'name': 'Face Token',
                    'symbol': 'FACE',
                },
            },
            {
                'id': 'base_quote_1',
                'type': 'token',
                'attributes': {
                    'address': '0x4200000000000000000000000000000000000006',
                    'name': 'Wrapped Ether',
                    'symbol': 'WETH',
                },
            },
        ],
    }


class FakeGeckoClient:
    def get_new_pools(self, network: str, page: int = 1) -> dict:
        assert network == 'base'
        return _fake_new_pools_response()

    def get_trending_pools(
        self,
        network: str,
        duration: str,
        page: int = 1,
    ) -> dict:
        assert network == 'base'
        assert duration in {'5m', '1h'}
        return _fake_new_pools_response()

    def search_pools(self, network: str, query: str, page: int = 1) -> dict:
        assert network == 'base'
        assert query
        return _fake_new_pools_response()


class FakeDexClient:
    def get_token_pairs(self, chain_id: str, token_addresses: list[str]) -> list[dict]:
        assert chain_id == 'base'
        assert token_addresses == ['0xface']
        created_at_ms = int((datetime.now(UTC) - timedelta(minutes=30)).timestamp() * 1000)
        return [
            {
                'chainId': 'base',
                'dexId': 'uniswap',
                'pairAddress': '0xdexpair1',
                'baseToken': {
                    'address': '0xface',
                    'name': 'Face Token',
                    'symbol': 'FACE',
                },
                'quoteToken': {
                    'address': '0x4200000000000000000000000000000000000006',
                    'name': 'Wrapped Ether',
                    'symbol': 'WETH',
                },
                'txns': {
                    'm5': {'buys': 20, 'sells': 5},
                    'h1': {'buys': 80, 'sells': 30},
                },
                'volume': {'m5': 5000, 'h1': 65000},
                'liquidity': {'usd': 150000},
                'pairCreatedAt': created_at_ms,
                'boosts': {'active': 2},
            }
        ]


def test_fresh_quality_only_returns_tokens_inside_window() -> None:
    service = TokenScoringService(settings=Settings())

    ranked = service.scan_tokens(strategy=Strategy.FRESH_QUALITY, limit=10)

    assert ranked
    assert all(token.age_minutes <= 360 for token in ranked)


def test_compare_tokens_orders_by_opportunity_score() -> None:
    service = TokenScoringService(settings=Settings())

    result = service.compare_tokens(
        token_addresses=[
            '0x4444444444444444444444444444444444444444',
            '0x1111111111111111111111111111111111111111',
        ],
        strategy=Strategy.HIGH_GREED_HIGH_RISK,
    )

    assert len(result.tokens) == 2
    assert result.tokens[0].opportunity_score >= result.tokens[1].opportunity_score


def test_fresh_quality_uses_geckoterminal_when_configured() -> None:
    service = TokenScoringService(
        settings=Settings(coingecko_api_key='test-key'),
        gecko_client=FakeGeckoClient(),
    )

    ranked = service.scan_tokens(strategy=Strategy.FRESH_QUALITY, limit=5)

    assert ranked
    assert ranked[0].symbol == 'FACE'
    assert ranked[0].data_source == 'coingecko_new_pools'


def test_fresh_quality_is_enriched_by_dexscreener() -> None:
    service = TokenScoringService(
        settings=Settings(coingecko_api_key='test-key'),
        gecko_client=FakeGeckoClient(),
        dex_client=FakeDexClient(),
    )

    ranked = service.scan_tokens(strategy=Strategy.FRESH_QUALITY, limit=5)

    assert ranked
    assert ranked[0].data_source == 'coingecko_new_pools+dexscreener_token_pairs'
    assert ranked[0].source_count == 2
    assert ranked[0].liquidity_usd == 150000.0
    assert 'DEX Screener shows 2 active boosts.' in ranked[0].warnings
