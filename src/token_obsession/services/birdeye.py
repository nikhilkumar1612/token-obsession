"""Small client for Birdeye trending and token overview endpoints."""

from __future__ import annotations

from typing import Any

import httpx

from token_obsession.core.config import Settings


class BirdeyeClientError(RuntimeError):
    """Raised when Birdeye cannot be used successfully."""


class BirdeyeClient:
    """Client for Birdeye trending and token overview lookups."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def get_trending_tokens(
        self,
        chain: str,
        sort_by: str,
        sort_type: str = "asc",
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Fetch trending tokens for one chain."""

        return self._get(
            "/defi/token_trending",
            headers={"x-chain": chain},
            params={
                "sort_by": sort_by,
                "sort_type": sort_type,
                "offset": offset,
                "limit": limit or self._settings.birdeye_max_trending_tokens,
            },
        )

    def get_token_overview(
        self,
        chain: str,
        address: str,
    ) -> dict[str, Any]:
        """Fetch one token overview."""

        return self._get(
            "/defi/token_overview",
            headers={"x-chain": chain},
            params={"address": address},
        )

    def _get(
        self,
        path: str,
        headers: dict[str, str],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._settings.birdeye_api_key:
            raise BirdeyeClientError(
                "TOKEN_OBSESSION_BIRDEYE_API_KEY is not configured.",
            )

        request_headers = {
            "accept": "application/json",
            "X-API-KEY": self._settings.birdeye_api_key,
            **headers,
        }

        with httpx.Client(
            base_url=self._settings.birdeye_base_url,
            headers=request_headers,
            timeout=self._settings.birdeye_timeout_seconds,
        ) as client:
            try:
                response = client.get(path, params=params)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise BirdeyeClientError(
                    f"Birdeye request failed for {path}: {exc}",
                ) from exc

        payload = response.json()
        if not isinstance(payload, dict):
            raise BirdeyeClientError("Unexpected Birdeye response shape.")
        if payload.get("success") is False:
            raise BirdeyeClientError(f"Birdeye returned an error payload for {path}.")
        return payload
