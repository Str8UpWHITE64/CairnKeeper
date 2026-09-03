"""Challenge progression must survive a route offline.

The challenge level is stated once, when the route is asked for, and never
again: /SubmitRun carries neither `curseLevel` nor `whipWagered`, even on a run
that wagered a whip at level 1000. Both have to be remembered per route or a
beaten challenge is banked as no challenge at all.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54908"
PLAYER = "76561198000000001"


def _backend(tmp_path: Path) -> OfflineBackend:
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    return OfflineBackend(tmp_path / "state", archive=Library(assets, tmp_path / "fx"))


def _play(backend: OfflineBackend, whip: str, curse: int, relic: str = "") -> dict:
    """A challenge run the way the client actually makes one."""
    first = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 0, "dungeonId": 0,
         "dungeonFloorNumber": 0, "playerId": PLAYER,
         "whipWagered": whip, "curseLevel": curse}, BASE
    )
    # The submission carries neither field -- this mirrors real captures.
    backend.submit_run({
        "success": 2, "playerId": PLAYER, "gameMode": "DGM_ADVENTURE",
        "routeId": first["routeID"], "dungeonId": first["dungeonID"],
        "dungeonFloorNumber": 2, "collectedRelicId": relic,
        "currency": {"essence": 0, "dungeonKeys": [1, 0, 0, 0]},
    })
    return first


def test_the_wagered_challenge_level_is_recorded(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    _play(backend, "Lightning", 1000)
    routes = backend.profiles.for_player(PLAYER).data["victoryRoutes"]
    assert routes[-1]["curseLevel"] == 1000, "read from the submission, not the route"
    assert routes[-1]["wageredWhipID"] == "Lightning"


def test_a_run_with_no_challenge_records_zero(tmp_path: Path) -> None:
    """54 of 58 real victory routes carry curseLevel 0."""
    backend = _backend(tmp_path)
    _play(backend, "Bamboo", 0)
    assert backend.profiles.for_player(PLAYER).data["victoryRoutes"][-1]["curseLevel"] == 0


def test_each_route_keeps_its_own_challenge_level(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    _play(backend, "Bamboo", 530)
    _play(backend, "Scales", 670)
    routes = backend.profiles.for_player(PLAYER).data["victoryRoutes"]
    assert [(r["wageredWhipID"], r["curseLevel"]) for r in routes[-2:]] == [
        ("Bamboo", 530), ("Scales", 670)
    ]


def test_challenge_level_survives_a_restart(tmp_path: Path) -> None:
    """The route is asked for in one session and finished in another."""
    backend = _backend(tmp_path)
    first = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 0, "dungeonId": 0,
         "dungeonFloorNumber": 0, "playerId": PLAYER,
         "whipWagered": "Ice", "curseLevel": 420}, BASE
    )
    reopened = _backend(tmp_path)
    reopened.submit_run({
        "success": 2, "playerId": PLAYER, "gameMode": "DGM_ADVENTURE",
        "routeId": first["routeID"], "dungeonId": first["dungeonID"],
        "dungeonFloorNumber": 2,
        "currency": {"essence": 0, "dungeonKeys": [0, 0, 0, 0]},
    })
    route = reopened.profiles.for_player(PLAYER).data["victoryRoutes"][-1]
    assert route["curseLevel"] == 420 and route["wageredWhipID"] == "Ice"


def test_finishing_the_tutorial_is_banked_and_acknowledged(tmp_path: Path) -> None:
    """A new account starts here, and until now nothing recorded that it had.

    `/CompleteTutorialDungeon` answered with a bare acknowledgment, which was
    harmless while every account had done the tutorial years ago. Profiles made
    new accounts real: the client got no receipt, so it ran the tutorial again,
    and then sat at "waiting for connection" against a server it was talking to
    perfectly well.

    The captured request and reply are a run submission and a run receipt --
    the tutorial awards the first dungeon key and the Tutorial relic, and is
    what marks the account as no longer new.
    """
    backend = _backend(tmp_path)
    player = "pa-newcomer"

    receipt = backend.complete_tutorial({
        "gameMode": "DGM_ADVENTURE", "playerId": player, "routeId": 0,
        "dungeonId": 0, "dungeonFloorNumber": 1, "lifetime": 110.17,
        "success": 2, "isSandbag": 0, "collectedRelicId": "Tutorial",
        "currency": {"essence": 0, "dungeonKeys": [1, 0, 0, 0]},
    })

    assert receipt["success"] is True
    assert receipt["userID"], "the client is told which account this was"
    assert receipt["currency"] == {"essence": 0, "dungeonKeys": [1, 0, 0, 0]}
    assert "maintenanceInfo" in receipt
    assert "attemptID" in receipt

    profile = backend.profiles.for_player(player).snapshot()
    assert profile["existingUser"] is True, "or the tutorial runs again"
    assert profile["currency"]["dungeonKeys"] == [1, 0, 0, 0]

    stored = json.loads(
        (tmp_path / "state" / "profiles" / f"{player}.json").read_text("utf-8"))
    assert [r["relicID"] for r in stored["relics"]] == ["Tutorial"]
