"""Bootstrap scoring engine with in-memory sample Base token data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from token_obsession.core.config import Settings
from token_obsession.core.models import (
    Chain,
    CompareTokensResult,
    ExplainTokenResult,
    Opportunity,
    Strategy,
    TokenSnapshot,
)


def _clamp(value: float, lower: float = 0.0, upper: float = 10.0) -> float:
    """Clamp a score into a bounded range."""

    return max(lower, min(value, upper))


class BootstrapScoringService:
    """Simple rules-based scorer used before real data sources are wired in."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._snapshots = self._build_sample_snapshots()

    def scan_tokens(
        self,
        strategy: Strategy,
        limit: int = 5,
        chain: Chain = Chain.BASE,
    ) -> list[Opportunity]:
        """Return ranked opportunities for a strategy."""

        ranked = [
            self._score_snapshot(snapshot=snapshot, strategy=strategy)
            for snapshot in self._eligible_snapshots(chain=chain, strategy=strategy)
        ]
        ranked.sort(key=lambda item: item.opportunity_score, reverse=True)
        return ranked[:limit]

    def explain_token(self, token_address: str, strategy: Strategy) -> ExplainTokenResult:
        """Return a detailed explanation for a single token."""

        snapshot = self._get_snapshot(token_address)
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
            self._score_snapshot(snapshot=self._get_snapshot(address), strategy=strategy)
            for address in token_addresses
        ]
        tokens.sort(key=lambda item: item.opportunity_score, reverse=True)
        return CompareTokensResult(strategy=strategy, tokens=tokens)

    def _eligible_snapshots(
        self,
        chain: Chain,
        strategy: Strategy,
    ) -> list[TokenSnapshot]:
        max_age_minutes = self.settings.fresh_window_hours * 60
        eligible: list[TokenSnapshot] = []
        for snapshot in self._snapshots:
            if snapshot.chain != chain:
                continue
            if snapshot.liquidity_usd < self.settings.min_liquidity_usd:
                continue
            if strategy == Strategy.FRESH_QUALITY and snapshot.age_minutes > max_age_minutes:
                continue
            if "sell_blocked" in snapshot.risk_flags or "honeypot" in snapshot.risk_flags:
                continue
            eligible.append(snapshot)
        return eligible

    def _get_snapshot(self, token_address: str) -> TokenSnapshot:
        for snapshot in self._snapshots:
            if snapshot.token_address.lower() == token_address.lower():
                return snapshot
        raise ValueError(f"Unknown token address: {token_address}")

    def _score_snapshot(self, snapshot: TokenSnapshot, strategy: Strategy) -> Opportunity:
        risk_score = self._risk_score(snapshot)
        confidence_score = self._confidence_score(snapshot)

        if strategy == Strategy.FRESH_QUALITY:
            opportunity_score = self._fresh_quality_score(snapshot, risk_score)
            reasons = [
                f"Seen on Base within the last {snapshot.age_minutes} minutes.",
                f"Liquidity is ${snapshot.liquidity_usd:,.0f}.",
                f"Holder growth over 1h is {snapshot.holder_growth_1h}.",
            ]
        elif strategy == Strategy.SAFER_MOMENTUM:
            opportunity_score = self._safer_momentum_score(snapshot, risk_score)
            reasons = [
                f"15m volume is ${snapshot.volume_15m_usd:,.0f}.",
                f"Volume acceleration is {snapshot.volume_acceleration:.1f}x.",
                f"Net buyer ratio is {snapshot.net_buyer_ratio:.2f}.",
            ]
        else:
            opportunity_score = self._high_greed_score(snapshot, risk_score)
            reasons = [
                f"Fast activity with {snapshot.buy_count_15m} buys in 15m.",
                f"Speculative acceleration is {snapshot.volume_acceleration:.1f}x.",
                "This strategy intentionally accepts more risk.",
            ]

        return Opportunity(
            chain=snapshot.chain,
            strategy=strategy,
            token_address=snapshot.token_address,
            pool_address=snapshot.pool_address,
            symbol=snapshot.symbol,
            name=snapshot.name,
            age_minutes=snapshot.age_minutes,
            liquidity_usd=snapshot.liquidity_usd,
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

    def _fresh_quality_score(self, snapshot: TokenSnapshot, risk_score: float) -> float:
        freshness = _clamp(10 - (snapshot.age_minutes / 36))
        liquidity = _clamp(snapshot.liquidity_usd / 30_000)
        holder_growth = _clamp(snapshot.holder_growth_1h / 20)
        return (
            (freshness * 0.35)
            + (liquidity * 0.25)
            + (holder_growth * 0.20)
            + ((10 - risk_score) * 0.20)
        )

    def _safer_momentum_score(self, snapshot: TokenSnapshot, risk_score: float) -> float:
        liquidity = _clamp(snapshot.liquidity_usd / 40_000)
        acceleration = _clamp(snapshot.volume_acceleration * 1.7)
        buyers = _clamp((snapshot.net_buyer_ratio + 1) * 5)
        return (
            (liquidity * 0.30)
            + (acceleration * 0.35)
            + (buyers * 0.20)
            + ((10 - risk_score) * 0.15)
        )

    def _high_greed_score(self, snapshot: TokenSnapshot, risk_score: float) -> float:
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
