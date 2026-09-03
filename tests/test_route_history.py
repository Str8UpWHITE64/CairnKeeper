"""Route history must accumulate, because everything else derives from it.

The server stores no relic collection, no unlocked-whip list and no challenge
flags. The client computes all of them from victoryRoutes
(GetUniqueRelicsEarned, GetUnlockedWhipByDungeonID,
IsAdventureRelicChallengeComplete). If routes stop accumulating, a player's
collection silently freezes while everything else still looks healthy.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402
from phantom_offline.profile import Profile  # noqa: E402

PLAYER = "76561198000000042"


def _route(route_id: int, whip: str, dungeon_id: int, relics, stage: int = 0) -> dict:
    return {
        "routeID": route_id,
        "wageredWhipID": whip,
        "dungeons": [
            {
                "dungeonID": dungeon_id,
                "routeStage": stage,
                "relicIDs": relics,
            }
        ],
    }


def _backend(tmp_path: Path) -> OfflineBackend:
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    return OfflineBackend(tmp_path / "state", archive=Library(assets, tmp_path / "fx"))


def test_relics_derive_from_route_history(tmp_path: Path) -> None:
    p = Profile(tmp_path / "p.json")
    p.record_victory(_route(1, "Bamboo", 100, ["SnakeHead", "GoldenEgg"]))
    p.record_victory(_route(2, "Ice", 200, ["Panda"]))
    assert p.relics_earned() == ["GoldenEgg", "Panda", "SnakeHead"]


def test_whips_derive_from_route_history(tmp_path: Path) -> None:
    p = Profile(tmp_path / "p.json")
    p.record_victory(_route(1, "Bamboo", 100, []))
    p.record_victory(_route(2, "Ice", 200, []))
    p.record_victory(_route(3, "Ice", 300, []))
    assert p.whips_used() == ["Bamboo", "Ice"]


def test_relics_are_deduplicated(tmp_path: Path) -> None:
    p = Profile(tmp_path / "p.json")
    p.record_victory(_route(1, "Bamboo", 100, ["Panda"]))
    p.record_victory(_route(2, "Ice", 200, ["Panda"]))
    assert p.relics_earned() == ["Panda"]


def test_later_floors_do_not_erase_earlier_dungeons(tmp_path: Path) -> None:
    """A route spans several dungeons; each submission reports one."""
    p = Profile(tmp_path / "p.json")
    p.record_victory(_route(7, "Ice", 100, ["SnakeHead"], stage=0))
    p.record_victory(_route(7, "Ice", 200, ["Panda"], stage=1))
    routes = p.snapshot()["victoryRoutes"]
    assert len(routes) == 1, "same route must not duplicate"
    assert len(routes[0]["dungeons"]) == 2
    assert p.relics_earned() == ["Panda", "SnakeHead"]


def test_relics_merge_within_one_dungeon(tmp_path: Path) -> None:
    p = Profile(tmp_path / "p.json")
    p.record_victory(_route(7, "Ice", 100, ["SnakeHead"]))
    p.record_victory(_route(7, "Ice", 100, ["GoldenEgg"]))
    assert p.relics_earned() == ["GoldenEgg", "SnakeHead"]


def test_route_without_id_is_ignored(tmp_path: Path) -> None:
    p = Profile(tmp_path / "p.json")
    p.record_victory({"routeID": 0, "dungeons": []})
    assert p.snapshot()["victoryRoutes"] == []


def test_finished_run_records_its_route(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    backend.submit_run(
        {
            "playerId": PLAYER,
            "routeId": 555,
            "dungeonId": 999,
            "dungeonFloorNumber": 1,
            "success": 2,
            "collectedRelicId": "DragonHead",
            "currency": {"essence": 0, "dungeonKeys": [1, 0, 0, 0]},
        }
    )
    profile = backend.profiles.for_player(PLAYER)
    assert profile.relics_earned() == ["DragonHead"]
    assert profile.snapshot()["victoryRoutes"][0]["routeID"] == 555


def test_mid_route_floor_records_nothing(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    backend.submit_run(
        {
            "playerId": PLAYER,
            "routeId": 556,
            "dungeonId": 999,
            "success": 1,
            "collectedRelicId": "DragonHead",
        }
    )
    assert backend.profiles.for_player(PLAYER).snapshot()["victoryRoutes"] == []


def test_history_survives_restart(tmp_path: Path) -> None:
    p = Profile(tmp_path / "p.json")
    p.record_victory(_route(1, "Void", 100, ["KingsBlade"]))
    assert Profile(tmp_path / "p.json").relics_earned() == ["KingsBlade"]


def test_death_without_a_completion_is_not_a_victory(tmp_path: Path) -> None:
    """Captured logins prove a pure-death route is discarded.

    Route 222645 ended in death with no temple completed and victoryRoutes
    stayed at 56.
    """
    backend = _backend(tmp_path)
    backend.submit_run(
        {"playerId": PLAYER, "routeId": 222645, "dungeonId": 1, "success": 0,
         "currency": {"essence": 0, "dungeonKeys": [1, 0, 0, 0]}}
    )
    profile = backend.profiles.for_player(PLAYER)
    assert profile.snapshot()["victoryRoutes"] == []
    # The keys are still banked, though - death does bank currency.
    assert profile.snapshot()["currency"]["dungeonKeys"][0] == 1


def test_completion_then_death_keeps_one_victory(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    for success, relic in ((2, "Panda"), (0, "")):
        backend.submit_run(
            {"playerId": PLAYER, "routeId": 205602, "dungeonId": 701203,
             "success": success, "collectedRelicId": relic,
             "currency": {"essence": 0, "dungeonKeys": [0, 0, 0, 0]}}
        )
    profile = backend.profiles.for_player(PLAYER)
    assert len(profile.snapshot()["victoryRoutes"]) == 1
    assert profile.relics_earned() == ["Panda"]
