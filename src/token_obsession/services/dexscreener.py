"""Small client for DEX Screener token-pair enrichment."""

from __future__ import annotations

from typing import Any

import httpx

from token_obsession.core.config import Settings


class DexScreenerClientError(RuntimeError):
    """Raised when DEX Screener cannot be used successfully."""


class DexScreenerClient:
    """Client for token pair lookups on DEX Screener."""

    max_addresses_per_request = 30

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get_token_pairs(
        self,
        chain_id: str,
        token_addresses: list[str],
    ) -> list[dict[str, Any]]:
        """Fetch pair data for up to many token addresses on one chain."""

        if not token_addresses:
            return []

        unique_addresses = list(dict.fromkeys(token_addresses))
        pairs: list[dict[str, Any]] = []
        for chunk_start in range(0, len(unique_addresses), self.max_addresses_per_request):
            chunk = unique_addresses[chunk_start : chunk_start + self.max_addresses_per_request]
            token_path = ",".join(chunk)
            payload = self._get(f"/tokens/v1/{chain_id}/{token_path}")
            pairs.extend(item for item in payload if isinstance(item, dict))
        return pairs

    def _get(self, path: str) -> list[dict[str, Any]]:
        with httpx.Client(
            base_url=self._settings.dexscreener_base_url,
            timeout=self._settings.dexscreener_timeout_seconds,
        ) as client:
            try:
                response = client.get(path)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise DexScreenerClientError(
                    f"DEX Screener request failed for {path}: {exc}",
                ) from exc

        payload = response.json()
        if not isinstance(payload, list):
            raise DexScreenerClientError("Unexpected DEX Screener response shape.")
        return payload
