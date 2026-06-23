"""Persistent Safe sell-proposal state for deduplication and reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from pydantic import BaseModel, Field

from token_obsession.core.config import Settings
from token_obsession.core.models import SellProposalRecord, SellProposalStatus


class _SellProposalStoragePayload(BaseModel):
    """JSON payload persisted for Safe sell proposals."""

    proposals: list[SellProposalRecord] = Field(default_factory=list)


class SellProposalStore:
    """Persist and update Safe sell proposals on local disk."""

    def __init__(
        self,
        settings: Settings,
        proposals_file_path: Path | None = None,
    ) -> None:
        self._proposals_file_path = proposals_file_path or settings.sell_proposals_file_path
        self._lock = Lock()

    def add(self, proposal: SellProposalRecord) -> SellProposalRecord:
        """Persist a newly submitted Safe proposal."""

        with self._lock:
            payload = self._load_payload()
            payload.proposals.append(proposal)
            self._write_payload(payload)
        return proposal

    def list(self) -> list[SellProposalRecord]:
        """Return all proposals, newest first."""

        with self._lock:
            proposals = list(self._load_payload().proposals)
        proposals.sort(key=lambda proposal: proposal.created_at, reverse=True)
        return proposals

    def pending_for_position(self, position_id: str) -> SellProposalRecord | None:
        """Return the newest pending proposal for one position."""

        return next(
            (
                proposal
                for proposal in self.list()
                if proposal.position_id == position_id
                and proposal.status == SellProposalStatus.PENDING
            ),
            None,
        )

    def latest_for_position(self, position_id: str) -> SellProposalRecord | None:
        """Return the newest proposal for one position."""

        return next(
            (proposal for proposal in self.list() if proposal.position_id == position_id),
            None,
        )

    def update_status(
        self,
        proposal_id: str,
        status: SellProposalStatus,
        execution_tx_hash: str | None = None,
        failure_reason: str | None = None,
    ) -> SellProposalRecord:
        """Update a proposal after checking its current Safe status."""

        with self._lock:
            payload = self._load_payload()
            for index, proposal in enumerate(payload.proposals):
                if proposal.proposal_id != proposal_id:
                    continue
                updated = proposal.model_copy(
                    update={
                        "status": status,
                        "execution_tx_hash": execution_tx_hash,
                        "failure_reason": self._normalize_optional_text(failure_reason),
                        "updated_at": datetime.now(UTC),
                    }
                )
                payload.proposals[index] = updated
                self._write_payload(payload)
                return updated
        raise ValueError(f"Unknown sell proposal id: {proposal_id}")

    def _load_payload(self) -> _SellProposalStoragePayload:
        path = self._proposals_file_path
        if not path.exists():
            return _SellProposalStoragePayload()
        return _SellProposalStoragePayload.model_validate_json(path.read_text(encoding="utf-8"))

    def _write_payload(self, payload: _SellProposalStoragePayload) -> None:
        path = self._proposals_file_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.parent / f"{path.name}.tmp"
        temp_path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        temp_path.replace(path)

    @staticmethod
    def _normalize_optional_text(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None
