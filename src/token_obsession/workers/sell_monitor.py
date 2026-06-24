"""Scheduled worker that evaluates positions and proposes Safe sell transactions."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime, timedelta

from token_obsession.core.config import Settings, get_settings
from token_obsession.core.models import (
    SellEvaluationConfig,
    SellProposalStatus,
    SellRecommendation,
    SellWorkerPositionResult,
    SellWorkerResultStatus,
    SellWorkerRunReport,
)
from token_obsession.services.positions import PositionStore
from token_obsession.services.safe_sell import SafeSellProposalService
from token_obsession.services.sell_evaluation import SellEvaluationService
from token_obsession.services.sell_proposals import SellProposalStore

logger = logging.getLogger(__name__)


class SellMonitorWorker:
    """Evaluate open positions and publish deduplicated Safe sell proposals."""

    def __init__(
        self,
        settings: Settings,
        position_store: PositionStore | None = None,
        evaluation_service: SellEvaluationService | None = None,
        proposal_service: SafeSellProposalService | None = None,
        proposal_store: SellProposalStore | None = None,
    ) -> None:
        self._settings = settings
        self._position_store = position_store or PositionStore(settings=settings)
        self._evaluation_service = evaluation_service or SellEvaluationService(settings=settings)
        self._proposal_service = proposal_service or SafeSellProposalService(settings=settings)
        self._proposal_store = proposal_store or SellProposalStore(settings=settings)

    def run_once(self) -> SellWorkerRunReport:
        """Run one complete reconciliation and sell-evaluation pass."""

        started_at = datetime.now(UTC)
        logger.info(
            "MONITOR RUN STARTED | started_at=%s | proposals_enabled=%s",
            started_at.isoformat(),
            self._settings.sell_proposals_enabled,
        )
        logger.info("MONITOR STAGE 1/3 | Reconciling existing Safe proposals.")
        self._reconcile_pending_proposals()

        logger.info("MONITOR STAGE 2/3 | Loading tracked open positions.")
        positions = self._position_store.list_positions()
        logger.info(
            "POSITIONS LOADED | count=%d | tokens=%s",
            len(positions),
            ",".join(position.symbol for position in positions) or "none",
        )
        config = self._evaluation_config()
        logger.info(
            "SELL RULES LOADED | take_profit=%s%% | stop_loss=%s%% | "
            "price_drop=%s%%/%s | volume_drop=%s%%/%s | required_market_signals=%d",
            config.take_profit_percent,
            config.stop_loss_percent,
            config.price_drop_percent,
            config.price_change_window.value,
            config.volume_drop_percent,
            config.volume_comparison_window.value,
            config.minimum_market_signals_for_sell,
        )
        logger.info("MONITOR STAGE 3/3 | Fetching market data and evaluating positions.")
        try:
            evaluation_report = self._evaluation_service.evaluate_open_positions(
                positions=positions,
                config=config,
            )
        except Exception as exc:
            logger.exception(
                "MONITOR RUN FAILED | stage=position_evaluation | error=%s",
                exc,
            )
            raise
        positions_by_id = {position.position_id: position for position in positions}
        results: list[SellWorkerPositionResult] = []

        for evaluation in evaluation_report.positions:
            position = positions_by_id[evaluation.position_id]
            logger.info(
                "POSITION EVALUATED | token=%s | position_id=%s | entry_price_usd=%s | "
                "current_price_usd=%s | pnl_percent=%s | price_change_percent=%s | "
                "volume_change_percent=%s | recommendation=%s | hard_triggers=%s | "
                "market_triggers=%s | warnings=%s",
                position.symbol,
                position.position_id,
                position.entry_price_usd,
                evaluation.current_price_usd,
                evaluation.unrealized_pnl_percent,
                evaluation.price_change_percent,
                evaluation.volume_change_percent,
                evaluation.recommendation.value,
                evaluation.hard_exit_triggers or "none",
                evaluation.market_exit_triggers or "none",
                evaluation.warnings or "none",
            )
            if evaluation.recommendation != SellRecommendation.SELL:
                logger.info(
                    "NO SELL ACTION | token=%s | recommendation=%s | reasons=%s",
                    position.symbol,
                    evaluation.recommendation.value,
                    evaluation.reasons or evaluation.warnings or "No matching sell triggers.",
                )
                results.append(
                    SellWorkerPositionResult(
                        position_id=position.position_id,
                        symbol=position.symbol,
                        recommendation=evaluation.recommendation,
                        status=SellWorkerResultStatus.NO_ACTION,
                        message="Sell rules did not trigger.",
                    )
                )
                continue

            pending = self._proposal_store.pending_for_position(position.position_id)
            if pending is not None:
                logger.warning(
                    "SELL SKIPPED | token=%s | reason=proposal_already_pending | safe_tx_hash=%s",
                    position.symbol,
                    pending.safe_tx_hash,
                )
                results.append(
                    SellWorkerPositionResult(
                        position_id=position.position_id,
                        symbol=position.symbol,
                        recommendation=evaluation.recommendation,
                        status=SellWorkerResultStatus.ALREADY_PENDING,
                        message="A Safe sell proposal is already pending.",
                        safe_tx_hash=pending.safe_tx_hash,
                    )
                )
                continue

            if self._in_reproposal_cooldown(position.position_id):
                logger.warning(
                    "SELL SKIPPED | token=%s | reason=reproposal_cooldown | "
                    "cooldown_minutes=%d",
                    position.symbol,
                    self._settings.sell_reproposal_cooldown_minutes,
                )
                results.append(
                    SellWorkerPositionResult(
                        position_id=position.position_id,
                        symbol=position.symbol,
                        recommendation=evaluation.recommendation,
                        status=SellWorkerResultStatus.COOLDOWN,
                        message="A failed or superseded proposal is still in cooldown.",
                    )
                )
                continue

            try:
                logger.warning(
                    "SELL SIGNAL TRIGGERED | token=%s | position_id=%s | hard_triggers=%s | "
                    "market_triggers=%s",
                    position.symbol,
                    position.position_id,
                    evaluation.hard_exit_triggers or "none",
                    evaluation.market_exit_triggers or "none",
                )
                logger.info("SAFE PROPOSAL REQUEST STARTED | token=%s", position.symbol)
                proposal = self._proposal_service.propose_position_sell(position)
                self._proposal_store.add(proposal)
            except Exception as exc:
                logger.exception(
                    "SELL ACTION REQUIRED | token=%s | position_id=%s | "
                    "recommendation=SELL | Safe transaction build/proposal failed: %s",
                    position.symbol,
                    position.position_id,
                    exc,
                )
                results.append(
                    SellWorkerPositionResult(
                        position_id=position.position_id,
                        symbol=position.symbol,
                        recommendation=evaluation.recommendation,
                        status=SellWorkerResultStatus.ERROR,
                        message=str(exc),
                    )
                )
                continue

            logger.warning(
                "SAFE SELL PROPOSAL CREATED | token=%s | position_id=%s | safe_tx_hash=%s",
                position.symbol,
                position.position_id,
                proposal.safe_tx_hash,
            )
            results.append(
                SellWorkerPositionResult(
                    position_id=position.position_id,
                    symbol=position.symbol,
                    recommendation=evaluation.recommendation,
                    status=SellWorkerResultStatus.PROPOSED,
                    message="Safe sell proposal created.",
                    safe_tx_hash=proposal.safe_tx_hash,
                )
            )

        completed_at = datetime.now(UTC)
        report = SellWorkerRunReport(
            started_at=started_at,
            completed_at=completed_at,
            positions_checked=len(positions),
            results=results,
        )
        logger.info(
            "MONITOR RUN COMPLETED | positions=%d | proposed=%d | errors=%d | "
            "duration_seconds=%.2f",
            report.positions_checked,
            sum(result.status == SellWorkerResultStatus.PROPOSED for result in results),
            sum(result.status == SellWorkerResultStatus.ERROR for result in results),
            (completed_at - started_at).total_seconds(),
        )
        return report

    def _reconcile_pending_proposals(self) -> None:
        pending_proposals = [
            proposal
            for proposal in self._proposal_store.list()
            if proposal.status == SellProposalStatus.PENDING
        ]
        logger.info("PROPOSAL RECONCILIATION | pending_count=%d", len(pending_proposals))
        for proposal in pending_proposals:
            try:
                logger.info(
                    "PROPOSAL STATUS CHECK STARTED | token=%s | safe_tx_hash=%s",
                    proposal.symbol,
                    proposal.safe_tx_hash,
                )
                chain_status = self._proposal_service.get_proposal_status(proposal)
                if chain_status.status == SellProposalStatus.PENDING:
                    logger.info(
                        "PROPOSAL STILL PENDING | token=%s | safe_tx_hash=%s",
                        proposal.symbol,
                        proposal.safe_tx_hash,
                    )
                    continue
                updated = self._proposal_store.update_status(
                    proposal_id=proposal.proposal_id,
                    status=chain_status.status,
                    execution_tx_hash=chain_status.execution_tx_hash,
                    failure_reason=chain_status.failure_reason,
                )
                if updated.status == SellProposalStatus.EXECUTED:
                    self._position_store.close_position(
                        position_id=updated.position_id,
                        close_reason=(
                            "Safe sell executed"
                            + (
                                f": {updated.execution_tx_hash}"
                                if updated.execution_tx_hash
                                else ""
                            )
                        ),
                    )
                    logger.warning(
                        "SAFE SELL EXECUTED | token=%s | position_id=%s | tx_hash=%s",
                        updated.symbol,
                        updated.position_id,
                        updated.execution_tx_hash,
                    )
                else:
                    logger.error(
                        "SELL ACTION REQUIRED | token=%s | position_id=%s | "
                        "proposal_status=%s | reason=%s",
                        updated.symbol,
                        updated.position_id,
                        updated.status.value,
                        updated.failure_reason,
                    )
            except Exception as exc:
                logger.exception(
                    "SELL ACTION REQUIRED | token=%s | position_id=%s | "
                    "proposal reconciliation failed: %s",
                    proposal.symbol,
                    proposal.position_id,
                    exc,
                )

    def _in_reproposal_cooldown(self, position_id: str) -> bool:
        latest = self._proposal_store.latest_for_position(position_id)
        if latest is None or latest.status not in {
            SellProposalStatus.FAILED,
            SellProposalStatus.SUPERSEDED,
        }:
            return False
        cooldown = timedelta(minutes=self._settings.sell_reproposal_cooldown_minutes)
        return datetime.now(UTC) < latest.updated_at + cooldown

    def _evaluation_config(self) -> SellEvaluationConfig:
        return SellEvaluationConfig(
            take_profit_percent=self._settings.sell_take_profit_percent,
            stop_loss_percent=self._settings.sell_stop_loss_percent,
            price_drop_percent=self._settings.sell_price_drop_percent,
            price_change_window=self._settings.sell_price_change_window,
            volume_drop_percent=self._settings.sell_volume_drop_percent,
            volume_comparison_window=self._settings.sell_volume_comparison_window,
            minimum_market_signals_for_sell=(self._settings.sell_minimum_market_signals),
        )


def build_worker(settings: Settings | None = None) -> SellMonitorWorker:
    """Build the production sell worker from environment-backed settings."""

    return SellMonitorWorker(settings=settings or get_settings())


def configure_logging() -> None:
    """Configure readable worker logs with a blank line between records."""

    handler = logging.StreamHandler()
    handler.terminator = "\n\n"
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def main() -> None:
    """Run the sell monitor once or continuously at the configured interval."""

    parser = argparse.ArgumentParser(description="Monitor positions for Safe sell signals.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one monitoring pass and exit.",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="Override TOKEN_OBSESSION_SELL_CHECK_INTERVAL_MINUTES.",
    )
    args = parser.parse_args()
    settings = get_settings()
    interval_minutes = args.interval_minutes or settings.sell_check_interval_minutes
    if interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")

    configure_logging()
    logger.info(
        "SELL MONITOR PROCESS STARTED | mode=%s | interval_minutes=%d | "
        "proposals_enabled=%s | positions_file=%s | proposals_file=%s",
        "once" if args.once else "continuous",
        interval_minutes,
        settings.sell_proposals_enabled,
        settings.positions_file_path,
        settings.sell_proposals_file_path,
    )
    worker = build_worker(settings=settings)
    while True:
        try:
            worker.run_once()
        except Exception as exc:
            logger.exception(
                "MONITOR CYCLE FAILED | error=%s | next_action=%s",
                exc,
                "exit" if args.once else "retry_after_interval",
            )
            if args.once:
                raise
        if args.once:
            return
        logger.info("Next sell check in %d minutes.", interval_minutes)
        try:
            time.sleep(interval_minutes * 60)
        except KeyboardInterrupt:
            logger.info("Sell worker stopped.")
            return


if __name__ == "__main__":
    main()
