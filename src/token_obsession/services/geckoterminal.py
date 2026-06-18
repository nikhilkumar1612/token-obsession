"""Small client for CoinGecko Onchain / GeckoTerminal endpoints."""

from __future__ import annotations

from typing import Any

import httpx

from token_obsession.core.config import Settings


class GeckoTerminalClientError(RuntimeError):
    """Raised when the GeckoTerminal API cannot be used successfully."""


class GeckoTerminalClient:
    """Official CoinGecko Onchain API client for Base pool discovery."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get_new_pools(
        self,
        network: str,
        page: int = 1,
    ) -> dict[str, Any]:
        """Fetch newest pools for a network."""

        return self._get(
            f"/onchain/networks/{network}/new_pools",
            params={
                "include": "base_token,quote_token,dex",
                "include_gt_community_data": "true",
                "page": page,
            },
        )

    def get_trending_pools(
        self,
        network: str,
        duration: str,
        page: int = 1,
    ) -> dict[str, Any]:
        """Fetch trending pools for a network and duration."""

        return self._get(
            f"/onchain/networks/{network}/trending_pools",
            params={
                "include": "base_token,quote_token,dex",
                "include_gt_community_data": "true",
                "duration": duration,
                "page": page,
            },
        )

    def search_pools(
        self,
        network: str,
        query: str,
        page: int = 1,
    ) -> dict[str, Any]:
        """Search pools and tokens by symbol, name, or token address."""

        return self._get(
            "/onchain/search/pools",
            params={
                "include": "base_token,quote_token,dex",
                "network": network,
                "page": page,
                "query": query,
            },
        )

    def _auth_header_name(self) -> str:
        """Return the correct auth header for the configured CoinGecko host."""

        if 'pro-api.coingecko.com' in self._settings.coingecko_base_url:
            return 'x-cg-pro-api-key'
        return 'x-cg-demo-api-key'

    def _get(
        self,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._settings.coingecko_api_key:
            raise GeckoTerminalClientError(
                "TOKEN_OBSESSION_COINGECKO_API_KEY is not configured.",
            )

        headers = {self._auth_header_name(): self._settings.coingecko_api_key}

        with httpx.Client(
            base_url=self._settings.coingecko_base_url,
            headers=headers,
            timeout=self._settings.coingecko_timeout_seconds,
        ) as client:
            try:
                response = client.get(path, params=params)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise GeckoTerminalClientError(
                    f"CoinGecko request failed for {path}: {exc}",
                ) from exc

        payload = response.json()
        if not isinstance(payload, dict):
            raise GeckoTerminalClientError(
                "Unexpected CoinGecko response shape.",
            )
        return payload
