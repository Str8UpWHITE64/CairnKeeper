"""The run receipt is what decides the cutscene, so it has to name the run.

Nothing is asked of the server between the last /SubmitRun of a route and the
relic cutscene in the hub. The client decides from that one reply plus what it
already holds, and if the reply is not the receipt it is waiting for, the
relic and whip turn up at the next login instead, derived from victoryRoutes.
That is exactly what happened: everything banked, nothing granted until the
game was restarted.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

PLAYER = "76561198000000042"


def _backend(tmp_path: Path) -> OfflineBackend:
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    return OfflineBackend(tmp_path / "state", archive=Library(assets, tmp_path / "fx"))


def _floor(floor: int, success: int, **extra) -> dict:
    return {
        "playerId": PLAYER,
        "gameMode": "DGM_ADVENTURE",
        "routeId": 18,
        "routeAttemptId": 0,
        "dungeonId": 928611,
        "dungeonFloorNumber": floor,
        "success": success,
        "leaderboardType": "DLT_SCORE",
        "currency": {"essence": 0, "dungeonKeys": [8, 0, 0, 0]},
        "runData": "QkFTRTY0LVBIQU5UT00tUkVDT1JESU5H",
        **extra,
    }


def test_attempt_id_is_the_route_attempt_not_our_run_count(tmp_path: Path) -> None:
    """In 103 captured receipts attemptID equals lastRunRouteInfo.routeAttemptID.

    102 say 0 in both places; the one that says 1 says it in both places. Ours
    said 0 in the route block and 68 on the receipt -- a count of every run
    this state directory had ever stored -- and the client did not treat that
    as the receipt for the run it had just submitted.
    """
    backend = _backend(tmp_path)
    receipts = [
        backend.submit_run(_floor(0, 1)),
        backend.submit_run(_floor(1, 1)),
        backend.submit_run(_floor(2, 2, collectedRelicId="SoldierSword")),
    ]

    for receipt in receipts:
        assert receipt["attemptID"] == 0
        assert isinstance(receipt["attemptID"], int)

    route_end = receipts[-1]
    assert route_end["lastRunRouteInfo"] is not None
    assert route_end["attemptID"] == route_end["lastRunRouteInfo"]["routeAttemptID"]
    assert route_end["attemptID"] != 3, "that is our run count, not the attempt"


def test_attempt_id_follows_the_attempt_the_client_names(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    receipt = backend.submit_run(_floor(2, 2, routeAttemptId=1))
    assert receipt["attemptID"] == 1
    assert receipt["lastRunRouteInfo"]["routeAttemptID"] == 1


def test_tutorial_receipt_says_attempt_zero(tmp_path: Path) -> None:
    """The one captured /CompleteTutorialDungeon receipt carries attemptID 0."""
    backend = _backend(tmp_path)
    receipt = backend.complete_tutorial({
        "gameMode": "DGM_ADVENTURE", "playerId": PLAYER, "routeId": 0,
        "dungeonId": 0, "dungeonFloorNumber": 1, "lifetime": 110.17,
        "success": 2, "isSandbag": 0, "collectedRelicId": "Tutorial",
        "currency": {"essence": 0, "dungeonKeys": [1, 0, 0, 0]},
    })
    assert receipt["attemptID"] == 0


def test_receipt_daily_block_names_the_board_the_run_was_scored_on(tmp_path: Path) -> None:
    """All 104 captured receipts say leaderboardType 0 for a DLT_SCORE run.

    The daily block captured from a live login says 1, and copying it into
    receipts was the other place ours disagreed with every live receipt.
    """
    backend = _backend(tmp_path)
    assert backend.submit_run(_floor(0, 1))["dailySubmissionResponse"]["leaderboardType"] == 0
    timed = backend.submit_run(_floor(1, 1, leaderboardType="DLT_TIME"))
    assert timed["dailySubmissionResponse"]["leaderboardType"] == 1
    unnamed = backend.submit_run({k: v for k, v in _floor(2, 2).items() if k != "leaderboardType"})
    assert unnamed["dailySubmissionResponse"]["leaderboardType"] == 0


def test_our_run_count_still_names_the_recording(tmp_path: Path) -> None:
    """The counter did not go away; it just stopped being sent."""
    backend = _backend(tmp_path)
    backend.submit_run(_floor(0, 1))
    backend.submit_run(_floor(1, 1))
    stored = sorted(p.name for p in (tmp_path / "assets").iterdir() if "runinfo" in p.name)
    assert stored == [
        "local__dungeon-928611-floor-0-runinfo-000001",
        "local__dungeon-928611-floor-1-runinfo-000002",
    ]
