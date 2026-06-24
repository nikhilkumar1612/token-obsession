import logging

from token_obsession.core.config import Settings
from token_obsession.core.models import (
    Chain,
    PositionCreate,
    PositionSellEvaluation,
    PositionStatus,
    PriceChangeWindow,
    SellEvaluationReport,
    SellProposalRecord,
    SellProposalStatus,
    SellRecommendation,
    SellWorkerResultStatus,
    VolumeComparisonWindow,
)
from token_obsession.services.positions import PositionStore
from token_obsession.services.safe_sell import (
    SafeProposalChainStatus,
    SafeSellProposalService,
)
from token_obsession.services.sell_proposals import SellProposalStore
from token_obsession.workers.sell_monitor import SellMonitorWorker, configure_logging


class FakeEvaluationService:
    def __init__(self, recommendation: SellRecommendation) -> None:
        self.recommendation = recommendation

    def evaluate_open_positions(self, positions, config) -> SellEvaluationReport:
        return SellEvaluationReport(
            config=config,
            positions=[
                PositionSellEvaluation(
                    position_id=position.position_id,
                    chain=Chain.BASE,
                    token_address=position.token_address,
                    symbol=position.symbol,
                    quantity=position.quantity,
                    entry_price_usd=position.entry_price_usd,
                    current_price_usd=0.3,
                    unrealized_pnl_percent=-28.57,
                    price_change_window=PriceChangeWindow.H1,
                    volume_comparison_window=VolumeComparisonWindow.M5_VS_H1,
                    recommendation=self.recommendation,
                    data_source="test",
                )
                for position in positions
            ],
        )


class FakeProposalService:
    def __init__(
        self,
        chain_status: SafeProposalChainStatus | None = None,
        error: Exception | None = None,
    ) -> None:
        self.chain_status = chain_status or SafeProposalChainStatus(SellProposalStatus.PENDING)
        self.error = error
        self.proposed_position_ids: list[str] = []

    def propose_position_sell(self, position) -> SellProposalRecord:
        self.proposed_position_ids.append(position.position_id)
        if self.error is not None:
            raise self.error
        return _proposal(position.position_id, position.symbol, position.token_address)

    def get_proposal_status(self, proposal) -> SafeProposalChainStatus:
        return self.chain_status


def _proposal(position_id: str, symbol: str, token_address: str) -> SellProposalRecord:
    return SellProposalRecord(
        proposal_id="proposal-1",
        position_id=position_id,
        token_address=token_address,
        symbol=symbol,
        safe_address="0x2222222222222222222222222222222222222222",
        safe_tx_hash="0x" + "ab" * 32,
        safe_nonce=1,
        quote_id="quote-1",
        from_amount=10000000000000,
        minimum_to_amount=40000000,
        output_token_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )


def _stores(tmp_path):
    settings = Settings(
        positions_file_path=tmp_path / "positions.json",
        sell_proposals_file_path=tmp_path / "sell-proposals.json",
    )
    position_store = PositionStore(settings=settings)
    position = position_store.add_position(
        PositionCreate(
            token_address="0xb20a4bd059f5914a2f8b9c18881c637f79efb7df",
            symbol="ADS",
            quantity=100,
            entry_price_usd=0.42,
        )
    )
    return settings, position_store, SellProposalStore(settings=settings), position


def test_position_amount_uses_token_decimals() -> None:
    assert (
        SafeSellProposalService._position_amount_in_base_units(
            quantity=100,
            decimals=11,
            sell_percentage=100,
        )
        == 10000000000000
    )


def test_worker_proposes_only_once_while_transaction_is_pending(tmp_path) -> None:
    settings, position_store, proposal_store, position = _stores(tmp_path)
    proposal_service = FakeProposalService()
    worker = SellMonitorWorker(
        settings=settings,
        position_store=position_store,
        evaluation_service=FakeEvaluationService(SellRecommendation.SELL),
        proposal_service=proposal_service,
        proposal_store=proposal_store,
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first.results[0].status == SellWorkerResultStatus.PROPOSED
    assert second.results[0].status == SellWorkerResultStatus.ALREADY_PENDING
    assert proposal_service.proposed_position_ids == [position.position_id]


def test_worker_closes_position_after_successful_safe_execution(tmp_path) -> None:
    settings, position_store, proposal_store, position = _stores(tmp_path)
    proposal_store.add(_proposal(position.position_id, position.symbol, position.token_address))
    proposal_service = FakeProposalService(
        chain_status=SafeProposalChainStatus(
            SellProposalStatus.EXECUTED,
            execution_tx_hash="0x" + "cd" * 32,
        )
    )
    worker = SellMonitorWorker(
        settings=settings,
        position_store=position_store,
        evaluation_service=FakeEvaluationService(SellRecommendation.SELL),
        proposal_service=proposal_service,
        proposal_store=proposal_store,
    )

    report = worker.run_once()

    assert report.positions_checked == 0
    assert position_store.list_positions() == []
    closed = position_store.list_positions(include_closed=True)[0]
    assert closed.status == PositionStatus.CLOSED
    assert "Safe sell executed" in (closed.close_reason or "")


def test_worker_logs_a_prominent_sell_error(tmp_path, caplog) -> None:
    settings, position_store, proposal_store, _ = _stores(tmp_path)
    worker = SellMonitorWorker(
        settings=settings,
        position_store=position_store,
        evaluation_service=FakeEvaluationService(SellRecommendation.SELL),
        proposal_service=FakeProposalService(error=RuntimeError("quote unavailable")),
        proposal_store=proposal_store,
    )

    with caplog.at_level(logging.ERROR):
        report = worker.run_once()

    assert report.results[0].status == SellWorkerResultStatus.ERROR
    assert "SELL ACTION REQUIRED" in caplog.text
    assert "quote unavailable" in caplog.text


def test_worker_logging_uses_double_newline_terminator() -> None:
    configure_logging()

    assert logging.getLogger().handlers[0].terminator == "\n\n"
