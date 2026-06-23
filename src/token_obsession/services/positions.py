"""Position storage for manually tracked token entries."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

from pydantic import BaseModel, Field

from token_obsession.core.config import Settings
from token_obsession.core.models import Position, PositionCreate, PositionStatus


class _PositionStoragePayload(BaseModel):
    """JSON payload persisted on disk for tracked positions."""

    positions: list[Position] = Field(default_factory=list)


class PositionStore:
    """Persist tracked positions to a local JSON file."""

    def __init__(
        self,
        settings: Settings,
        positions_file_path: Path | None = None,
    ) -> None:
        self._positions_file_path = positions_file_path or settings.positions_file_path
        self._lock = Lock()

    def add_position(self, position_create: PositionCreate) -> Position:
        """Store a newly created position and return the saved record."""

        timestamp = datetime.now(UTC)
        position = Position(
            position_id=uuid4().hex,
            **position_create.model_dump(),
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._lock:
            payload = self._load_payload()
            payload.positions.append(position)
            self._write_payload(payload)
        return position

    def list_positions(self, include_closed: bool = False) -> list[Position]:
        """Return tracked positions, newest first."""

        with self._lock:
            positions = list(self._load_payload().positions)

        if not include_closed:
            positions = [
                position for position in positions if position.status == PositionStatus.OPEN
            ]

        positions.sort(key=lambda position: position.entry_time, reverse=True)
        return positions

    def close_position(
        self,
        position_id: str,
        exit_price_usd: float | None = None,
        closed_at: datetime | None = None,
        close_reason: str | None = None,
    ) -> Position:
        """Mark an existing position as closed and persist the update."""

        normalized_reason = self._normalize_optional_text(close_reason)
        updated_at = datetime.now(UTC)
        closed_timestamp = closed_at or updated_at

        with self._lock:
            payload = self._load_payload()
            for index, position in enumerate(payload.positions):
                if position.position_id != position_id:
                    continue
                if position.status == PositionStatus.CLOSED:
                    raise ValueError(f"Position already closed: {position_id}")

                updated_position = position.model_copy(
                    update={
                        "status": PositionStatus.CLOSED,
                        "exit_price_usd": exit_price_usd,
                        "closed_at": closed_timestamp,
                        "close_reason": normalized_reason,
                        "updated_at": updated_at,
                    },
                )
                payload.positions[index] = updated_position
                self._write_payload(payload)
                return updated_position

        raise ValueError(f"Unknown position id: {position_id}")

    def _load_payload(self) -> _PositionStoragePayload:
        path = self._positions_file_path
        if not path.exists():
            return _PositionStoragePayload()
        return _PositionStoragePayload.model_validate_json(path.read_text(encoding="utf-8"))

    def _write_payload(self, payload: _PositionStoragePayload) -> None:
        path = self._positions_file_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.parent / f"{path.name}.tmp"
        temp_path.write_text(
            payload.model_dump_json(indent=2, exclude_computed_fields=True),
            encoding="utf-8",
        )
        temp_path.replace(path)

    @staticmethod
    def _normalize_optional_text(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None
