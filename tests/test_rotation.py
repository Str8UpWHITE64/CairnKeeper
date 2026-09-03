"""Fresh offline runs should vary, and must never dead-end.

Substituting a partially archived temple would strand the player on a floor we
cannot serve, so only fully archived temples are offered for a new run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54908"


def _temple(dungeon: int, floor: int, total: int) -> dict:
    return {
        "dungeonID": dungeon,
        "dungeonFloorNumber": floor,
        "dungeonFloorTotalCount": total,
        "dungeonSeed": dungeon * 1000 + floor,
        "layoutDownloadURL": f"https://dungeons.wiby.net/v/d/dungeon-{dungeon}-floor-{floor}-layout-x",
        "ghostRuns": [],
    }


def _build(tmp_path: Path, temples: list[tuple[int, int, int]]) -> Library:
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)

    records = []
    for dungeon, floor, total in temples:
        (assets / f"v__dungeon-{dungeon}-floor-{floor}-layout-x").write_bytes(b"L")
        records.append(
            json.dumps({"status": 200, "response_body": _temple(dungeon, floor, total)})
        )
    (fixtures / "GetDungeon.jsonl").write_text("\n".join(records) + "\n", encoding="utf-8")
    return Library(assets, fixtures)


def test_complete_dungeons_detected(tmp_path: Path) -> None:
    lib = _build(tmp_path, [(100, 0, 2), (100, 1, 2), (200, 0, 3)])
    assert lib.complete_dungeons() == [100]


def test_fresh_runs_rotate(tmp_path: Path) -> None:
    lib = _build(tmp_path, [(100, 0, 1), (200, 0, 1), (300, 0, 1)])
    picked = [lib.any_for_floor(0, pick=i).dungeon_id for i in range(6)]
    assert picked == [100, 200, 300, 100, 200, 300]


def test_incomplete_temple_not_offered_for_fresh_run(tmp_path: Path) -> None:
    # 200 is missing floor 1 of 3, so a new run must not start there.
    lib = _build(tmp_path, [(100, 0, 1), (200, 0, 3)])
    assert lib.complete_dungeons() == [100]
    assert lib.any_for_floor(0, pick=0).dungeon_id == 100
    assert lib.any_for_floor(0, pick=1).dungeon_id == 100


def test_backend_keeps_one_temple_across_a_run(tmp_path: Path) -> None:
    lib = _build(tmp_path, [(100, 0, 2), (100, 1, 2), (300, 0, 2), (300, 1, 2)])
    backend = OfflineBackend(tmp_path / "state", archive=lib)

    first = backend.get_dungeon({"dungeonId": 0, "dungeonFloorNumber": 0}, BASE)
    # Floor 1 arrives with the dungeon id we just handed out, so it resolves
    # exactly rather than substituting again.
    second = backend.get_dungeon(
        {"dungeonId": first["dungeonID"], "dungeonFloorNumber": 1}, BASE
    )
    assert second["dungeonID"] == first["dungeonID"]


def test_backend_rotates_between_runs(tmp_path: Path) -> None:
    lib = _build(tmp_path, [(100, 0, 1), (300, 0, 1)])
    backend = OfflineBackend(tmp_path / "state", archive=lib)
    a = backend.get_dungeon({"dungeonId": 0, "dungeonFloorNumber": 0}, BASE)
    b = backend.get_dungeon({"dungeonId": 0, "dungeonFloorNumber": 0}, BASE)
    assert a["dungeonID"] != b["dungeonID"]


def test_no_archive_returns_none(tmp_path: Path) -> None:
    lib = _build(tmp_path, [])
    assert lib.any_for_floor(0) is None


def test_area_maps_to_the_measured_difficulty_rating() -> None:
    """0/20/40/60 -> areas 1/2/3/4, measured against the live server."""
    from phantom_offline.harvest import AREA_DIFFICULTY

    assert AREA_DIFFICULTY == {1: 0, 2: 20, 3: 40, 4: 60}


def test_harvester_rejects_a_difficulty_that_yields_empty_temples(tmp_path: Path) -> None:
    """Anything outside the measured tiers returns areaID 0 with no floors."""
    import pytest

    from phantom_offline.harvest import Harvester

    for bad in (30, 100, 160, 300):
        with pytest.raises(ValueError, match="empty placeholder"):
            Harvester(tmp_path, difficulty=bad)


def test_eighty_is_a_valid_tier(tmp_path: Path) -> None:
    """diff 80 returns area 4 with FOUR floors - the deepest valid tier."""
    from phantom_offline.harvest import DIFFICULTY_TIERS, Harvester

    assert 80 in DIFFICULTY_TIERS
    assert Harvester(tmp_path, difficulty=80).difficulty == 80


def test_harvester_rejects_an_unknown_area(tmp_path: Path) -> None:
    import pytest

    from phantom_offline.harvest import Harvester

    with pytest.raises(ValueError, match="area must be one of"):
        Harvester(tmp_path, area=9)


def test_empty_temple_detection() -> None:
    """A drained pool returns areaID 0, one floor, no players."""
    from phantom_offline.harvest import is_empty_temple

    assert is_empty_temple({"dungeonFloorTotalCount": 0, "playerCount": 0})
    assert is_empty_temple({"dungeonFloorTotalCount": 1, "playerCount": 0})
    assert not is_empty_temple({"dungeonFloorTotalCount": 3, "playerCount": 178})


def test_empty_streak_limit_is_small() -> None:
    """102 consecutive empties went unnoticed once. Never again."""
    from phantom_offline.harvest import EMPTY_STREAK_LIMIT

    assert EMPTY_STREAK_LIMIT <= 10


def test_a_state_file_missing_a_key_still_serves(tmp_path: Path) -> None:
    """Defaults must apply per key, not only when there is no file at all.

    A state.json written by an older build, cut short by a crash, or trimmed by
    hand used to be returned as-is, so the first /GetDungeon died on
    KeyError('routeCounter'). The server runs detached, so that reached the
    player as a connection failure with nothing to explain it.
    """
    (tmp_path / "state.json").write_text(
        json.dumps({"templeRotation": 3}), encoding="utf-8")

    backend = OfflineBackend(tmp_path)
    assert backend.state["routeCounter"] == 1
    assert backend.state["templeRotation"] == 3, "what was saved still wins"
    assert backend.state["dungeons"] == {}

    response = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 0, "routeStage": 0,
         "dungeonId": 0, "dungeonFloorNumber": 0}, BASE)
    assert response.get("routeID"), "a run must still start"


def test_unreadable_state_falls_back_to_defaults(tmp_path: Path) -> None:
    (tmp_path / "state.json").write_text("{ truncated", encoding="utf-8")
    assert OfflineBackend(tmp_path).state["routeCounter"] == 1
