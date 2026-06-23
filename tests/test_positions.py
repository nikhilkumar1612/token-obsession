from datetime import UTC, datetime

import pytest

from token_obsession.core.config import Settings
from token_obsession.core.models import PositionCreate, PositionStatus, Strategy
from token_obsession.services.positions import PositionStore


def test_add_position_persists_to_disk(tmp_path) -> None:
    positions_file_path = tmp_path / "positions.json"
    store = PositionStore(settings=Settings(positions_file_path=positions_file_path))

    created = store.add_position(
        PositionCreate(
            token_address="0xFACE",
            symbol="face",
            name="Face Token",
            quantity=1250,
            entry_price_usd=0.42,
            strategy=Strategy.FRESH_QUALITY,
            notes="Manual buy from agent suggestion",
        )
    )

    reloaded_store = PositionStore(settings=Settings(positions_file_path=positions_file_path))
    positions = reloaded_store.list_positions()

    assert positions_file_path.exists()
    assert len(positions) == 1
    assert positions[0].position_id == created.position_id
    assert positions[0].token_address == "0xface"
    assert positions[0].symbol == "FACE"
    assert positions[0].cost_basis_usd == 525.0


def test_close_position_hides_closed_positions_by_default(tmp_path) -> None:
    positions_file_path = tmp_path / "positions.json"
    store = PositionStore(settings=Settings(positions_file_path=positions_file_path))
    position = store.add_position(
        PositionCreate(
            token_address="0xface",
            symbol="FACE",
            quantity=100,
            entry_price_usd=2.0,
        )
    )

    closed_at = datetime(2026, 6, 22, 9, 30, tzinfo=UTC)
    closed = store.close_position(
        position_id=position.position_id,
        exit_price_usd=3.0,
        closed_at=closed_at,
        close_reason="Take profit",
    )

    assert closed.status == PositionStatus.CLOSED
    assert closed.closed_at == closed_at
    assert closed.realized_pnl_percent == 50.0
    assert store.list_positions() == []

    all_positions = store.list_positions(include_closed=True)
    assert len(all_positions) == 1
    assert all_positions[0].close_reason == "Take profit"


def test_close_position_requires_known_position_id(tmp_path) -> None:
    positions_file_path = tmp_path / "positions.json"
    store = PositionStore(settings=Settings(positions_file_path=positions_file_path))

    with pytest.raises(ValueError, match="Unknown position id"):
        store.close_position(position_id="missing-position")
