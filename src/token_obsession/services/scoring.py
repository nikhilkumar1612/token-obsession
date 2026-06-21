"""Scoring engine with GeckoTerminal discovery plus DEX and Birdeye signals."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

from token_obsession.core.config import Settings
from token_obsession.core.models import (
    Chain,
    CompareTokensResult,
    ExplainTokenResult,
    Opportunity,
    Strategy,
    TokenSnapshot,
)
from token_obsession.services.birdeye import (
    BirdeyeClient,
    BirdeyeClientError,
)
from token_obsession.services.dexscreener import (
    DexScreenerClient,
    DexScreenerClientError,
)
from token_obsession.services.geckoterminal import (
    GeckoTerminalClient,
    GeckoTerminalClientError,
)


def _clamp(value: float, lower: float = 0.0, upper: float = 10.0) -> float:
    """Clamp a score into a bounded range."""

    return max(lower, min(value, upper))


class TokenScoringService:
    """Rules-based scorer with live discovery and pair enrichment where available."""

    def __init__(
        self,
        settings: Settings,
        gecko_client: GeckoTerminalClient | None = None,
        dex_client: DexScreenerClient | None = None,
        birdeye_client: BirdeyeClient | None = None,
    ) -> None:
        self.settings = settings
        self._sample_snapshots = self._build_sample_snapshots()
        self._snapshot_cache = {
            snapshot.token_address.lower(): snapshot for snapshot in self._sample_snapshots
        }
        self._gecko_client = gecko_client or GeckoTerminalClient(settings=settings)
        self._dex_client = dex_client or DexScreenerClient(settings=settings)
        self._birdeye_client = birdeye_client or BirdeyeClient(settings=settings)

    def scan_tokens(
        self,
        strategy: Strategy,
        limit: int = 5,
        chain: Chain = Chain.BASE,
    ) -> list[Opportunity]:
        """Return ranked opportunities for a strategy."""

        snapshots = self._snapshots_for_strategy(chain=chain, strategy=strategy)
        ranked = [
            self._score_snapshot(snapshot=snapshot, strategy=strategy)
            for snapshot in self._eligible_snapshots(
                chain=chain,
                strategy=strategy,
                snapshots=snapshots,
            )
        ]
        ranked.sort(key=lambda item: item.opportunity_score, reverse=True)
        return ranked[:limit]

    def explain_token(
        self,
        token_address: str,
        strategy: Strategy,
    ) -> ExplainTokenResult:
        """Return a detailed explanation for a single token."""

        snapshot = self._get_snapshot(token_address, strategy=strategy)
        opportunity = self._score_snapshot(snapshot=snapshot, strategy=strategy)
        summary = (
            f"{snapshot.symbol} ranks for {strategy.value} because liquidity is "
            f"${snapshot.liquidity_usd:,.0f}, 15m volume acceleration is "
            f"{snapshot.volume_acceleration:.1f}x, and the current risk score is "
            f"{opportunity.risk_score:.1f}/10."
        )
        return ExplainTokenResult(
            opportunity=opportunity,
            risk_flags=snapshot.risk_flags,
            summary=summary,
        )

    def compare_tokens(
        self,
        token_addresses: list[str],
        strategy: Strategy,
    ) -> CompareTokensResult:
        """Compare several tokens under one strategy."""

        tokens = [
            self._score_snapshot(
                snapshot=self._get_snapshot(address, strategy=strategy),
                strategy=strategy,
            )
            for address in token_addresses
        ]
        tokens.sort(key=lambda item: item.opportunity_score, reverse=True)
        return CompareTokensResult(strategy=strategy, tokens=tokens)

    def _snapshots_for_strategy(
        self,
        chain: Chain,
        strategy: Strategy,
    ) -> list[TokenSnapshot]:
        if chain != Chain.BASE:
            return list(self._sample_snapshots)

        try:
            live_snapshots = self._fetch_live_snapshots(strategy=strategy)
        except GeckoTerminalClientError:
            live_snapshots = []

        if live_snapshots:
            for snapshot in live_snapshots:
                self._snapshot_cache[snapshot.token_address.lower()] = snapshot
            return live_snapshots

        return list(self._sample_snapshots)

    def _eligible_snapshots(
        self,
        chain: Chain,
        strategy: Strategy,
        snapshots: list[TokenSnapshot],
    ) -> list[TokenSnapshot]:
        max_age_minutes = self.settings.fresh_window_hours * 60
        eligible: list[TokenSnapshot] = []
        for snapshot in snapshots:
            if snapshot.chain != chain:
                continue
            if snapshot.liquidity_usd < self.settings.min_liquidity_usd:
                continue
            if strategy == Strategy.FRESH_QUALITY:
                if snapshot.age_minutes > max_age_minutes:
                    continue
                if "unknown_pair_age" in snapshot.risk_flags:
                    continue
            if "sell_blocked" in snapshot.risk_flags:
                continue
            if "honeypot" in snapshot.risk_flags:
                continue
            eligible.append(snapshot)
        return eligible

    def _get_snapshot(
        self,
        token_address: str,
        strategy: Strategy,
    ) -> TokenSnapshot:
        cached = self._snapshot_cache.get(token_address.lower())
        if cached is not None:
            return cached

        try:
            live_snapshot = self._fetch_snapshot_by_token(token_address=token_address)
        except (DexScreenerClientError, GeckoTerminalClientError):
            live_snapshot = None

        if live_snapshot is not None:
            self._snapshot_cache[token_address.lower()] = live_snapshot
            return live_snapshot

        for snapshot in self._sample_snapshots:
            if snapshot.token_address.lower() == token_address.lower():
                return snapshot

        raise ValueError(
            f"Unknown token address for strategy {strategy.value}: {token_address}",
        )

    def _fetch_live_snapshots(self, strategy: Strategy) -> list[TokenSnapshot]:
        snapshots: list[TokenSnapshot] = []
        birdeye_future: Future[dict[str, dict[str, Any]]] | None = None

        with ThreadPoolExecutor(max_workers=self._scan_provider_worker_count()) as executor:
            if self.settings.birdeye_api_key:
                birdeye_future = executor.submit(
                    self._birdeye_tokens_for_strategy,
                    strategy,
                )

            try:
                responses, data_source = self._gecko_responses_for_strategy(
                    strategy=strategy,
                    executor=executor,
                )
            except GeckoTerminalClientError:
                responses = []
                data_source = ""

        seen_tokens: set[str] = set()
        for response in responses:
            normalized = self._normalize_gecko_response(
                response=response,
                data_source=data_source,
            )
            for snapshot in normalized:
                token_key = snapshot.token_address.lower()
                if token_key in seen_tokens:
                    continue
                seen_tokens.add(token_key)
                snapshots.append(snapshot)

        try:
            snapshots = self._enrich_with_dexscreener(snapshots)
        except DexScreenerClientError:
            pass

        if birdeye_future is None:
            return snapshots

        try:
            birdeye_tokens = birdeye_future.result()
            return self._merge_birdeye_tokens(
                snapshots=snapshots,
                strategy=strategy,
                birdeye_tokens=birdeye_tokens,
            )
        except (BirdeyeClientError, DexScreenerClientError):
            return snapshots

    def _gecko_responses_for_strategy(
        self,
        strategy: Strategy,
        executor: ThreadPoolExecutor | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        if strategy == Strategy.FRESH_QUALITY:
            data_source = "coingecko_new_pools"

            def fetch_page(page: int) -> dict[str, Any]:
                return self._gecko_client.get_new_pools(
                    network=Chain.BASE.value,
                    page=page,
                )

        elif strategy in {
            Strategy.SAFER_MOMENTUM,
            Strategy.ESTABLISHED_TRENDING_24H,
        }:
            data_source = "coingecko_trending_1h"

            def fetch_page(page: int) -> dict[str, Any]:
                return self._gecko_client.get_trending_pools(
                    network=Chain.BASE.value,
                    duration="1h",
                    page=page,
                )

        else:
            data_source = "coingecko_trending_5m"

            def fetch_page(page: int) -> dict[str, Any]:
                return self._gecko_client.get_trending_pools(
                    network=Chain.BASE.value,
                    duration="5m",
                    page=page,
                )

        pages = list(range(1, self.settings.coingecko_max_pages + 1))
        if executor is None or len(pages) == 1:
            return ([fetch_page(page) for page in pages], data_source)

        futures = {page: executor.submit(fetch_page, page) for page in pages}
        responses = [futures[page].result() for page in pages]
        return responses, data_source

    def _fetch_snapshot_by_token(
        self,
        token_address: str,
    ) -> TokenSnapshot | None:
        response = self._gecko_client.search_pools(
            network=Chain.BASE.value,
            query=token_address,
            page=1,
        )
        snapshots = self._normalize_gecko_response(
            response=response,
            data_source="coingecko_search",
        )
        for snapshot in snapshots:
            if snapshot.token_address.lower() == token_address.lower():
                try:
                    return self._enrich_with_dexscreener([snapshot])[0]
                except DexScreenerClientError:
                    return snapshot

        dex_snapshots = self._snapshots_from_dex_only([token_address])
        if dex_snapshots:
            return dex_snapshots[0]
        return None

    def _normalize_gecko_response(
        self,
        response: dict[str, Any],
        data_source: str,
    ) -> list[TokenSnapshot]:
        included = {
            item["id"]: item.get("attributes", {})
            for item in response.get("included", [])
            if isinstance(item, dict) and "id" in item
        }

        snapshots: list[TokenSnapshot] = []
        for pool in response.get("data", []):
            if not isinstance(pool, dict):
                continue

            attributes = pool.get("attributes", {})
            relationships = pool.get("relationships", {})
            if not isinstance(attributes, dict) or not isinstance(relationships, dict):
                continue

            base_token_ref = relationships.get("base_token", {}).get("data", {}).get("id")
            quote_token_ref = relationships.get("quote_token", {}).get("data", {}).get("id")
            base_token = included.get(base_token_ref, {})
            quote_token = included.get(quote_token_ref, {})

            token_address = str(base_token.get("address") or "")
            pool_address = str(attributes.get("address") or "")
            symbol = str(base_token.get("symbol") or "")
            name = str(base_token.get("name") or "")
            if not token_address or not pool_address or not symbol or not name:
                continue

            created_at = self._parse_datetime(attributes.get("pool_created_at"))
            transactions = attributes.get("transactions", {})
            volume_usd = attributes.get("volume_usd", {})
            buyers_h1 = self._to_int(transactions.get("h1", {}).get("buyers"))
            sellers_h1 = self._to_int(transactions.get("h1", {}).get("sellers"))
            buyers_m15 = self._to_int(transactions.get("m15", {}).get("buyers"))
            sellers_m15 = self._to_int(transactions.get("m15", {}).get("sellers"))
            reserve_in_usd = self._to_float(attributes.get("reserve_in_usd"))
            volume_15m_usd = self._to_float(volume_usd.get("m15"))
            volume_1h_usd = self._to_float(volume_usd.get("h1"))
            suspicious_reports = self._to_int(attributes.get("community_sus_report"))
            quote_symbol = str(quote_token.get("symbol") or "")
            warnings = self._warnings_for_pool(
                reserve_in_usd=reserve_in_usd,
                community_sus_report=suspicious_reports,
                quote_symbol=quote_symbol,
            )
            risk_flags = self._risk_flags_for_pool(
                reserve_in_usd=reserve_in_usd,
                community_sus_report=suspicious_reports,
            )

            snapshots.append(
                TokenSnapshot(
                    chain=Chain.BASE,
                    data_source=data_source,
                    token_address=token_address,
                    pool_address=pool_address,
                    symbol=symbol,
                    name=name,
                    first_seen_at=created_at,
                    liquidity_usd=max(reserve_in_usd, 0.0),
                    price_usd=self._price_from_gecko_pool(attributes),
                    volume_15m_usd=volume_15m_usd,
                    volume_1h_usd=volume_1h_usd,
                    volume_acceleration=self._volume_acceleration(
                        volume_15m_usd=volume_15m_usd,
                        volume_1h_usd=volume_1h_usd,
                    ),
                    buy_count_15m=self._to_int(transactions.get("m15", {}).get("buys")),
                    sell_count_15m=self._to_int(transactions.get("m15", {}).get("sells")),
                    holder_growth_1h=max(buyers_h1 - sellers_h1, 0),
                    net_buyer_ratio=self._net_buyer_ratio(
                        buyers=buyers_m15,
                        sellers=sellers_m15,
                    ),
                    risk_flags=risk_flags,
                    warnings=warnings,
                    source_count=1,
                ),
            )

        return snapshots

    def _enrich_with_dexscreener(
        self,
        snapshots: list[TokenSnapshot],
    ) -> list[TokenSnapshot]:
        if not snapshots:
            return []

        token_addresses = [snapshot.token_address for snapshot in snapshots]
        pairs = self._dex_client.get_token_pairs(Chain.BASE.value, token_addresses)
        best_pairs = self._best_pairs_by_token(pairs)

        enriched: list[TokenSnapshot] = []
        for snapshot in snapshots:
            pair = best_pairs.get(snapshot.token_address.lower())
            if pair is None:
                enriched.append(snapshot)
                continue
            enriched.append(self._merge_snapshot_with_dex(snapshot, pair))
        return enriched

    def _enrich_with_birdeye(
        self,
        snapshots: list[TokenSnapshot],
        strategy: Strategy,
    ) -> list[TokenSnapshot]:
        if not self.settings.birdeye_api_key:
            return snapshots

        birdeye_tokens = self._birdeye_tokens_for_strategy(strategy=strategy)
        return self._merge_birdeye_tokens(
            snapshots=snapshots,
            strategy=strategy,
            birdeye_tokens=birdeye_tokens,
        )

    def _birdeye_tokens_for_strategy(
        self,
        strategy: Strategy,
    ) -> dict[str, dict[str, Any]]:
        sort_by, sort_type = self._birdeye_sort_config(strategy=strategy)
        response = self._birdeye_client.get_trending_tokens(
            chain=Chain.BASE.value,
            sort_by=sort_by,
            sort_type=sort_type,
            limit=self.settings.birdeye_max_trending_tokens,
        )
        return self._normalize_birdeye_trending(response)

    def _merge_birdeye_tokens(
        self,
        snapshots: list[TokenSnapshot],
        strategy: Strategy,
        birdeye_tokens: dict[str, dict[str, Any]],
    ) -> list[TokenSnapshot]:
        if not birdeye_tokens:
            return snapshots

        enriched: list[TokenSnapshot] = []
        remaining_tokens = dict(birdeye_tokens)
        for snapshot in snapshots:
            birdeye_token = remaining_tokens.pop(snapshot.token_address.lower(), None)
            if birdeye_token is None:
                enriched.append(snapshot)
                continue
            enriched.append(self._merge_snapshot_with_birdeye(snapshot, birdeye_token))

        if not remaining_tokens or strategy == Strategy.FRESH_QUALITY:
            return enriched

        backfill_snapshots = self._snapshots_from_dex_only(list(remaining_tokens))
        for snapshot in backfill_snapshots:
            birdeye_token = remaining_tokens.pop(snapshot.token_address.lower(), None)
            if birdeye_token is None:
                enriched.append(snapshot)
                continue
            enriched.append(self._merge_snapshot_with_birdeye(snapshot, birdeye_token))

        return enriched

    def _normalize_birdeye_trending(
        self,
        response: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        data = response.get("data", {})
        if isinstance(data, dict):
            tokens = data.get("tokens", [])
        else:
            tokens = []

        normalized: dict[str, dict[str, Any]] = {}
        for token in tokens:
            if not isinstance(token, dict):
                continue
            token_address = str(token.get("address") or token.get("tokenAddress") or "")
            if not token_address:
                continue
            normalized[token_address.lower()] = token
        return normalized

    def _birdeye_sort_config(
        self,
        strategy: Strategy,
    ) -> tuple[str, str]:
        if strategy == Strategy.FRESH_QUALITY:
            return ("rank", "asc")
        if strategy == Strategy.ESTABLISHED_TRENDING_24H:
            return ("volume24hUSD", "desc")
        return ("volume24hUSD", "desc")

    def _snapshots_from_dex_only(
        self,
        token_addresses: list[str],
    ) -> list[TokenSnapshot]:
        pairs = self._dex_client.get_token_pairs(Chain.BASE.value, token_addresses)
        best_pairs = self._best_pairs_by_token(pairs)
        snapshots: list[TokenSnapshot] = []
        for token_address in token_addresses:
            pair = best_pairs.get(token_address.lower())
            if pair is None:
                continue
            snapshots.append(
                self._snapshot_from_dex_pair(
                    pair,
                    data_source="dexscreener_token_pairs",
                ),
            )
        return snapshots

    def _best_pairs_by_token(
        self,
        pairs: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        best_pairs: dict[str, dict[str, Any]] = {}
        for pair in pairs:
            base_token = pair.get("baseToken", {})
            if not isinstance(base_token, dict):
                continue
            token_address = str(base_token.get("address") or "")
            if not token_address:
                continue
            token_key = token_address.lower()
            existing = best_pairs.get(token_key)
            if existing is None or self._pair_rank_key(pair) > self._pair_rank_key(existing):
                best_pairs[token_key] = pair
        return best_pairs

    def _pair_rank_key(self, pair: dict[str, Any]) -> tuple[int, float, float]:
        quote_symbol = str(pair.get("quoteToken", {}).get("symbol") or "")
        quote_priority = 1 if quote_symbol in self._core_quote_symbols() else 0
        liquidity = self._to_float(pair.get("liquidity", {}).get("usd"))
        volume_h1 = self._to_float(pair.get("volume", {}).get("h1"))
        return (quote_priority, liquidity, volume_h1)

    def _merge_snapshot_with_dex(
        self,
        snapshot: TokenSnapshot,
        pair: dict[str, Any],
    ) -> TokenSnapshot:
        liquidity_usd = max(
            snapshot.liquidity_usd,
            self._to_float(pair.get("liquidity", {}).get("usd")),
        )
        dex_price_usd = self._to_float(pair.get("priceUsd"))
        price_usd = dex_price_usd if dex_price_usd > 0 else snapshot.price_usd
        volume_15m_usd = snapshot.volume_15m_usd
        volume_1h_usd = max(
            snapshot.volume_1h_usd,
            self._to_float(pair.get("volume", {}).get("h1")),
        )
        m5_volume = self._to_float(pair.get("volume", {}).get("m5"))
        if volume_15m_usd <= 0 and m5_volume > 0:
            volume_15m_usd = m5_volume * 3

        buy_count_15m = snapshot.buy_count_15m
        sell_count_15m = snapshot.sell_count_15m
        buys_m5 = self._to_int(pair.get("txns", {}).get("m5", {}).get("buys"))
        sells_m5 = self._to_int(pair.get("txns", {}).get("m5", {}).get("sells"))
        if buy_count_15m <= 0 and buys_m5 > 0:
            buy_count_15m = buys_m5 * 3
        if sell_count_15m <= 0 and sells_m5 > 0:
            sell_count_15m = sells_m5 * 3

        warnings = list(snapshot.warnings)
        risk_flags = list(snapshot.risk_flags)
        active_boosts = self._to_int(pair.get("boosts", {}).get("active"))
        if active_boosts > 0:
            warnings.append(f"DEX Screener shows {active_boosts} active boosts.")
            risk_flags.append("dex_boosted")

        dex_quote_symbol = str(pair.get("quoteToken", {}).get("symbol") or "")
        if dex_quote_symbol and dex_quote_symbol not in self._core_quote_symbols():
            warnings.append(
                f"Quoted against {dex_quote_symbol}, not a core Base quote asset.",
            )

        pair_created_at = self._parse_unix_ms(pair.get("pairCreatedAt"))
        first_seen_at = snapshot.first_seen_at
        if pair_created_at is not None:
            first_seen_at = min(snapshot.first_seen_at, pair_created_at)
        net_buyer_ratio = snapshot.net_buyer_ratio
        if snapshot.buy_count_15m <= 0 and snapshot.sell_count_15m <= 0:
            net_buyer_ratio = self._net_buyer_ratio(
                buyers=buy_count_15m,
                sellers=sell_count_15m,
            )

        return TokenSnapshot(
            chain=snapshot.chain,
            data_source=f"{snapshot.data_source}+dexscreener_token_pairs",
            token_address=snapshot.token_address,
            pool_address=str(pair.get("pairAddress") or snapshot.pool_address),
            symbol=snapshot.symbol,
            name=snapshot.name,
            first_seen_at=first_seen_at,
            liquidity_usd=liquidity_usd,
            price_usd=price_usd,
            volume_15m_usd=volume_15m_usd,
            volume_1h_usd=volume_1h_usd,
            volume_acceleration=self._volume_acceleration(
                volume_15m_usd=volume_15m_usd,
                volume_1h_usd=volume_1h_usd,
            ),
            buy_count_15m=buy_count_15m,
            sell_count_15m=sell_count_15m,
            holder_growth_1h=snapshot.holder_growth_1h,
            net_buyer_ratio=net_buyer_ratio,
            risk_flags=self._dedupe_strings(risk_flags),
            warnings=self._dedupe_strings(warnings),
            source_count=max(snapshot.source_count, 2),
        )

    def _merge_snapshot_with_birdeye(
        self,
        snapshot: TokenSnapshot,
        birdeye_token: dict[str, Any],
    ) -> TokenSnapshot:
        liquidity_usd = max(
            snapshot.liquidity_usd,
            self._to_float(birdeye_token.get("liquidity")),
        )
        birdeye_price_usd = self._first_positive_float(
            birdeye_token.get("priceUsd"),
            birdeye_token.get("price"),
        )
        return TokenSnapshot(
            chain=snapshot.chain,
            data_source=f"{snapshot.data_source}+birdeye_trending",
            token_address=snapshot.token_address,
            pool_address=snapshot.pool_address,
            symbol=snapshot.symbol,
            name=snapshot.name,
            first_seen_at=snapshot.first_seen_at,
            liquidity_usd=liquidity_usd,
            price_usd=birdeye_price_usd or snapshot.price_usd,
            volume_15m_usd=snapshot.volume_15m_usd,
            volume_1h_usd=snapshot.volume_1h_usd,
            volume_acceleration=snapshot.volume_acceleration,
            buy_count_15m=snapshot.buy_count_15m,
            sell_count_15m=snapshot.sell_count_15m,
            holder_growth_1h=snapshot.holder_growth_1h,
            net_buyer_ratio=snapshot.net_buyer_ratio,
            risk_flags=snapshot.risk_flags,
            warnings=snapshot.warnings,
            source_count=snapshot.source_count + 1,
        )

    def _snapshot_from_dex_pair(
        self,
        pair: dict[str, Any],
        data_source: str,
    ) -> TokenSnapshot:
        base_token = pair.get("baseToken", {})
        quote_token = pair.get("quoteToken", {})
        token_address = str(base_token.get("address") or "")
        symbol = str(base_token.get("symbol") or "")
        name = str(base_token.get("name") or "")
        liquidity_usd = self._to_float(pair.get("liquidity", {}).get("usd"))
        price_usd = self._first_positive_float(pair.get("priceUsd"))
        volume_h1_usd = self._to_float(pair.get("volume", {}).get("h1"))
        m5_volume = self._to_float(pair.get("volume", {}).get("m5"))
        volume_15m_usd = m5_volume * 3 if m5_volume > 0 else 0.0
        buy_count_15m = self._to_int(pair.get("txns", {}).get("m5", {}).get("buys")) * 3
        sell_count_15m = self._to_int(pair.get("txns", {}).get("m5", {}).get("sells")) * 3
        quote_symbol = str(quote_token.get("symbol") or "")
        warnings: list[str] = []
        risk_flags: list[str] = []
        if liquidity_usd < self.settings.min_liquidity_usd * 2:
            warnings.append("Liquidity history is still shallow.")
            risk_flags.append("low_liquidity")
        if quote_symbol and quote_symbol not in self._core_quote_symbols():
            warnings.append(
                f"Quoted against {quote_symbol}, not a core Base quote asset.",
            )
        active_boosts = self._to_int(pair.get("boosts", {}).get("active"))
        if active_boosts > 0:
            warnings.append(f"DEX Screener shows {active_boosts} active boosts.")
            risk_flags.append("dex_boosted")

        pair_created_at = self._parse_unix_ms(pair.get("pairCreatedAt"))
        first_seen_at = pair_created_at or datetime.now(UTC)
        if pair_created_at is None:
            warnings.append("DEX Screener did not provide pair creation time.")
            risk_flags.append("unknown_pair_age")

        return TokenSnapshot(
            chain=Chain.BASE,
            data_source=data_source,
            token_address=token_address,
            pool_address=str(pair.get("pairAddress") or ""),
            symbol=symbol,
            name=name,
            first_seen_at=first_seen_at,
            liquidity_usd=liquidity_usd,
            price_usd=price_usd,
            volume_15m_usd=volume_15m_usd,
            volume_1h_usd=volume_h1_usd,
            volume_acceleration=self._volume_acceleration(
                volume_15m_usd=volume_15m_usd,
                volume_1h_usd=volume_h1_usd,
            ),
            buy_count_15m=buy_count_15m,
            sell_count_15m=sell_count_15m,
            holder_growth_1h=max(buy_count_15m - sell_count_15m, 0),
            net_buyer_ratio=self._net_buyer_ratio(
                buyers=buy_count_15m,
                sellers=sell_count_15m,
            ),
            risk_flags=self._dedupe_strings(risk_flags),
            warnings=self._dedupe_strings(warnings),
            source_count=1,
        )

    def _warnings_for_pool(
        self,
        reserve_in_usd: float,
        community_sus_report: int,
        quote_symbol: str,
    ) -> list[str]:
        warnings: list[str] = []
        if reserve_in_usd < self.settings.min_liquidity_usd * 2:
            warnings.append("Liquidity history is still shallow.")
        if community_sus_report > 0:
            warnings.append(
                f"GeckoTerminal community suspicious reports: {community_sus_report}.",
            )
        if quote_symbol and quote_symbol not in self._core_quote_symbols():
            warnings.append(
                f"Quoted against {quote_symbol}, not a core Base quote asset.",
            )
        return warnings

    def _risk_flags_for_pool(
        self,
        reserve_in_usd: float,
        community_sus_report: int,
    ) -> list[str]:
        risk_flags: list[str] = []
        if reserve_in_usd < self.settings.min_liquidity_usd * 2:
            risk_flags.append("low_liquidity")
        if community_sus_report > 0:
            risk_flags.append("community_sus_report")
        return risk_flags

    def _score_snapshot(
        self,
        snapshot: TokenSnapshot,
        strategy: Strategy,
    ) -> Opportunity:
        risk_score = self._risk_score(snapshot)
        confidence_score = self._confidence_score(snapshot)

        if strategy == Strategy.FRESH_QUALITY:
            opportunity_score = self._fresh_quality_score(snapshot, risk_score)
            reasons = [
                f"Seen on Base within the last {snapshot.age_minutes} minutes.",
                f"Liquidity is ${snapshot.liquidity_usd:,.0f}.",
                f"Holder growth proxy over 1h is {snapshot.holder_growth_1h}.",
            ]
        elif strategy == Strategy.SAFER_MOMENTUM:
            opportunity_score = self._safer_momentum_score(snapshot, risk_score)
            reasons = [
                f"15m volume is ${snapshot.volume_15m_usd:,.0f}.",
                f"Volume acceleration is {snapshot.volume_acceleration:.1f}x.",
                f"Net buyer ratio is {snapshot.net_buyer_ratio:.2f}.",
            ]
        elif strategy == Strategy.ESTABLISHED_TRENDING_24H:
            opportunity_score = self._established_trending_score(snapshot, risk_score)
            reasons = [
                f"1h volume is ${snapshot.volume_1h_usd:,.0f}.",
                f"Liquidity is ${snapshot.liquidity_usd:,.0f}.",
                f"Base pair has stayed live for {snapshot.age_minutes} minutes.",
            ]
        else:
            opportunity_score = self._high_greed_score(snapshot, risk_score)
            reasons = [
                f"Fast activity with {snapshot.buy_count_15m} buys in 15m.",
                f"Speculative acceleration is {snapshot.volume_acceleration:.1f}x.",
                "This strategy intentionally accepts more risk.",
            ]

        if "birdeye_trending" in snapshot.data_source:
            reasons.append("Birdeye trending flow also picked up this token on Base.")

        return Opportunity(
            chain=snapshot.chain,
            strategy=strategy,
            data_source=snapshot.data_source,
            token_address=snapshot.token_address,
            pool_address=snapshot.pool_address,
            symbol=snapshot.symbol,
            name=snapshot.name,
            age_minutes=snapshot.age_minutes,
            liquidity_usd=snapshot.liquidity_usd,
            price_usd=snapshot.price_usd,
            volume_15m_usd=snapshot.volume_15m_usd,
            volume_1h_usd=snapshot.volume_1h_usd,
            volume_acceleration=snapshot.volume_acceleration,
            opportunity_score=round(opportunity_score, 2),
            risk_score=round(risk_score, 2),
            confidence_score=round(confidence_score, 2),
            reasons=reasons,
            warnings=snapshot.warnings,
            source_count=snapshot.source_count,
            updated_at=snapshot.updated_at,
        )

    def _fresh_quality_score(
        self,
        snapshot: TokenSnapshot,
        risk_score: float,
    ) -> float:
        freshness = _clamp(10 - (snapshot.age_minutes / 36))
        liquidity = _clamp(snapshot.liquidity_usd / 30_000)
        holder_growth = _clamp(snapshot.holder_growth_1h / 20)
        return (
            (freshness * 0.35)
            + (liquidity * 0.25)
            + (holder_growth * 0.20)
            + ((10 - risk_score) * 0.20)
        )

    def _safer_momentum_score(
        self,
        snapshot: TokenSnapshot,
        risk_score: float,
    ) -> float:
        liquidity = _clamp(snapshot.liquidity_usd / 40_000)
        acceleration = _clamp(snapshot.volume_acceleration * 1.7)
        buyers = _clamp((snapshot.net_buyer_ratio + 1) * 5)
        return (
            (liquidity * 0.30)
            + (acceleration * 0.35)
            + (buyers * 0.20)
            + ((10 - risk_score) * 0.15)
        )

    def _established_trending_score(
        self,
        snapshot: TokenSnapshot,
        risk_score: float,
    ) -> float:
        liquidity = _clamp(snapshot.liquidity_usd / 75_000)
        hourly_volume = _clamp(snapshot.volume_1h_usd / 120_000)
        age_stability = _clamp(snapshot.age_minutes / 720)
        buyers = _clamp((snapshot.net_buyer_ratio + 1) * 5)
        return (
            (liquidity * 0.30)
            + (hourly_volume * 0.25)
            + (age_stability * 0.20)
            + (buyers * 0.10)
            + ((10 - risk_score) * 0.15)
        )

    def _high_greed_score(
        self,
        snapshot: TokenSnapshot,
        risk_score: float,
    ) -> float:
        acceleration = _clamp(snapshot.volume_acceleration * 2.0)
        trade_heat = _clamp(snapshot.buy_count_15m / 12)
        degen_bonus = _clamp(risk_score)
        return (
            (acceleration * 0.45)
            + (trade_heat * 0.30)
            + (degen_bonus * 0.15)
            + (_clamp(snapshot.liquidity_usd / 60_000) * 0.10)
        )

    def _risk_score(self, snapshot: TokenSnapshot) -> float:
        base_risk = 2.5
        base_risk += 1.5 if snapshot.liquidity_usd < 50_000 else 0
        base_risk += 1.2 if snapshot.net_buyer_ratio < 0 else 0
        base_risk += len(snapshot.risk_flags) * 1.4
        base_risk += 1.0 if snapshot.age_minutes < 20 else 0
        return _clamp(base_risk)

    def _confidence_score(self, snapshot: TokenSnapshot) -> float:
        source_bonus = _clamp(snapshot.source_count * 2.2)
        data_quality_bonus = _clamp(snapshot.liquidity_usd / 35_000)
        age_bonus = _clamp(snapshot.age_minutes / 45)
        return _clamp((source_bonus * 0.45) + (data_quality_bonus * 0.35) + (age_bonus * 0.20))

    def _build_sample_snapshots(self) -> list[TokenSnapshot]:
        now = datetime.now(UTC)
        return [
            TokenSnapshot(
                data_source="bootstrap",
                token_address="0x1111111111111111111111111111111111111111",
                pool_address="0xaaaa111111111111111111111111111111111111",
                symbol="ALPHA",
                name="Alpha Base",
                first_seen_at=now - timedelta(minutes=48),
                liquidity_usd=184000,
                volume_15m_usd=96000,
                volume_1h_usd=245000,
                volume_acceleration=4.9,
                buy_count_15m=188,
                sell_count_15m=73,
                holder_growth_1h=96,
                net_buyer_ratio=0.44,
                risk_flags=[],
                warnings=[],
                source_count=3,
            ),
            TokenSnapshot(
                data_source="bootstrap",
                token_address="0x2222222222222222222222222222222222222222",
                pool_address="0xbbbb222222222222222222222222222222222222",
                symbol="BOLT",
                name="Bolt Surge",
                first_seen_at=now - timedelta(minutes=17),
                liquidity_usd=42000,
                volume_15m_usd=118000,
                volume_1h_usd=159000,
                volume_acceleration=8.1,
                buy_count_15m=274,
                sell_count_15m=141,
                holder_growth_1h=62,
                net_buyer_ratio=0.32,
                risk_flags=["lp_unlock_unknown"],
                warnings=["Liquidity history is still shallow."],
                source_count=2,
            ),
            TokenSnapshot(
                data_source="bootstrap",
                token_address="0x3333333333333333333333333333333333333333",
                pool_address="0xcccc333333333333333333333333333333333333",
                symbol="CORA",
                name="Cora Velocity",
                first_seen_at=now - timedelta(hours=2, minutes=11),
                liquidity_usd=268000,
                volume_15m_usd=87000,
                volume_1h_usd=332000,
                volume_acceleration=3.6,
                buy_count_15m=121,
                sell_count_15m=66,
                holder_growth_1h=84,
                net_buyer_ratio=0.29,
                risk_flags=[],
                warnings=[],
                source_count=3,
            ),
            TokenSnapshot(
                data_source="bootstrap",
                token_address="0x4444444444444444444444444444444444444444",
                pool_address="0xdddd444444444444444444444444444444444444",
                symbol="DICE",
                name="Dice Rocket",
                first_seen_at=now - timedelta(minutes=9),
                liquidity_usd=21000,
                volume_15m_usd=143000,
                volume_1h_usd=143000,
                volume_acceleration=9.8,
                buy_count_15m=341,
                sell_count_15m=227,
                holder_growth_1h=31,
                net_buyer_ratio=0.18,
                risk_flags=["ownership_not_renounced"],
                warnings=["Very early token with extreme speculative behavior."],
                source_count=2,
            ),
            TokenSnapshot(
                data_source="bootstrap",
                token_address="0x5555555555555555555555555555555555555555",
                pool_address="0xeeee555555555555555555555555555555555555",
                symbol="FUSE",
                name="Fuse Layer",
                first_seen_at=now - timedelta(hours=7),
                liquidity_usd=152000,
                volume_15m_usd=42000,
                volume_1h_usd=173000,
                volume_acceleration=2.4,
                buy_count_15m=83,
                sell_count_15m=49,
                holder_growth_1h=51,
                net_buyer_ratio=0.26,
                risk_flags=[],
                warnings=[],
                source_count=2,
            ),
        ]

    def _price_from_gecko_pool(
        self,
        attributes: dict[str, Any],
    ) -> float | None:
        return self._first_positive_float(
            attributes.get("base_token_price_usd"),
            attributes.get("price_in_usd"),
        )

    def _scan_provider_worker_count(self) -> int:
        return max(1, self.settings.coingecko_max_pages + int(bool(self.settings.birdeye_api_key)))

    def _first_positive_float(self, *raw_values: Any) -> float | None:
        for raw_value in raw_values:
            value = self._to_float(raw_value)
            if value > 0:
                return value
        return None

    def _core_quote_symbols(self) -> set[str]:
        return {"WETH", "USDC", "cbBTC"}

    def _dedupe_strings(self, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    def _parse_datetime(self, raw_value: Any) -> datetime:
        if isinstance(raw_value, str) and raw_value.endswith("Z"):
            return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        if isinstance(raw_value, str):
            return datetime.fromisoformat(raw_value)
        return datetime.now(UTC)

    def _parse_unix_ms(self, raw_value: Any) -> datetime | None:
        try:
            return datetime.fromtimestamp(int(raw_value) / 1000, tz=UTC)
        except (TypeError, ValueError, OSError):
            return None

    def _to_float(self, raw_value: Any) -> float:
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return 0.0

    def _to_int(self, raw_value: Any) -> int:
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            return 0

    def _volume_acceleration(
        self,
        volume_15m_usd: float,
        volume_1h_usd: float,
    ) -> float:
        if volume_15m_usd <= 0:
            return 0.0
        hourly_quarter_average = max(volume_1h_usd / 4, 1.0)
        return volume_15m_usd / hourly_quarter_average

    def _net_buyer_ratio(self, buyers: int, sellers: int) -> float:
        total = buyers + sellers
        if total == 0:
            return 0.0
        return (buyers - sellers) / total
