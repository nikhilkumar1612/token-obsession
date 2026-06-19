from token_obsession.core.config import Settings
from token_obsession.services.geckoterminal import GeckoTerminalClient


def test_demo_host_uses_demo_header() -> None:
    client = GeckoTerminalClient(
        Settings(
            coingecko_api_key="demo-key",
            coingecko_base_url="https://api.coingecko.com/api/v3",
        ),
    )

    assert client._auth_header_name() == "x-cg-demo-api-key"


def test_pro_host_uses_pro_header() -> None:
    client = GeckoTerminalClient(
        Settings(
            coingecko_api_key="pro-key",
            coingecko_base_url="https://pro-api.coingecko.com/api/v3",
        ),
    )

    assert client._auth_header_name() == "x-cg-pro-api-key"
