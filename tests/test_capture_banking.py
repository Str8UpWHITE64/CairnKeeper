"""A live session must bank what it produces, without anyone remembering to.

Forwarding preserves the traffic and the archiver downloads whatever the CDN
referenced. Neither covers content that only travels upward:

  * a temple the client builds because the service had none to give
  * the player's own run, which is a recording nobody can recreate

Both were merged in by hand while this was being built. Anything that depends
on someone remembering a step is a step that gets missed.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54908"
LAYOUT = base64.b64encode(b'{"numWings":2,"roomInfos":[]}').decode()
RUN = base64.b64encode(b"recording" * 500).decode()

# What the live service answered when the client asked for a new temple:
# a real id, and no layout because none existed yet.
LIVE_GETDUNGEON = {
    "dungeonID": 747841, "dungeonFloorNumber": 0, "gameMode": 1,
    "areaID": 1, "dungeonSeed": -691251143, "dungeonLayoutType": 0,
    "dungeonFloorType": 0, "dungeonFloorTotalCount": 0,
    "layoutDownloadURL": "", "ghostRuns": [], "routeID": 222919,
}
LIVE_UPLOAD = {
    "dungeonId": 747841, "dungeonFloorNumber": 0, "savedLayoutData": LAYOUT,
    "dungeonLayoutType": 3, "dungeonFloorType": 1, "dungeonFloorCount": 3,
    "dungeonSettingsIndex": 0, "gameMode": "DGM_ADVENTURE",
}
LIVE_RUN = {
    "dungeonId": 747841, "dungeonFloorNumber": 0, "success": 2,
    "runData": RUN, "playerId": "76561198000000001", "routeAttemptId": 4,
}


def _backend(tmp_path: Path):
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    lib = Library(assets, tmp_path / "fx")
    return OfflineBackend(tmp_path / "state", archive=lib), lib


def _session(backend: OfflineBackend) -> None:
    """The exchanges a capture session sees, in order."""
    backend.observe_capture("/GetDungeon", None, LIVE_GETDUNGEON)
    backend.observe_capture("/SubmitDungeonLayout", LIVE_UPLOAD, {"accepted": True})
    backend.observe_capture("/SubmitRun", LIVE_RUN, {"success": True})


def test_a_temple_built_online_is_banked(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    _session(backend)
    temple = lib.lookup(747841, 0)
    assert temple is not None and temple.layout


def test_it_is_marked_real_and_may_be_pooled(tmp_path: Path) -> None:
    """The live service assigned the id, so this is genuine content."""
    backend, lib = _backend(tmp_path)
    _session(backend)
    assert lib.temples[(747841, 0)].origin == "client-online"
    assert (747841, 0) in lib.poolable()


def test_the_banked_temple_describes_its_own_shape(tmp_path: Path) -> None:
    """The response was written before the temple existed, so it stated none.

    Replaying a layout with no shape crashes the client on level load.
    """
    backend, lib = _backend(tmp_path)
    _session(backend)
    response = lib.temples[(747841, 0)].response
    assert response["dungeonLayoutType"] == 3
    assert response["dungeonFloorType"] == 1
    assert response["dungeonFloorTotalCount"] == 3


def test_the_players_own_run_is_kept_as_a_phantom(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    _session(backend)
    assert len(lib.temples[(747841, 0)].ghosts) == 1


def test_the_temple_is_playable_offline_afterwards(tmp_path: Path) -> None:
    """The whole point: capture, then play, with nothing in between."""
    backend, lib = _backend(tmp_path)
    _session(backend)

    offline = OfflineBackend(
        tmp_path / "state", archive=Library(tmp_path / "assets", tmp_path / "fx")
    )
    served = offline.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 0, "dungeonId": 747841,
         "dungeonFloorNumber": 0, "playerId": "76561198000000002"}, BASE
    )
    assert served["layoutDownloadURL"], "the temple replays"
    assert served["dungeonLayoutType"] == 3, "with the shape it was built at"
    assert len(served["ghostRuns"]) == 1, "and the phantom is there"


def test_a_temple_the_cdn_already_gave_us_is_not_overwritten(tmp_path: Path) -> None:
    """An upload must not clobber the genuine article we downloaded."""
    backend, lib = _backend(tmp_path)
    name = "version_128__2026-08-27__dungeon-747841-floor-0-layout-real"
    (tmp_path / "assets" / name).write_text("from-the-cdn", encoding="utf-8")
    lib.record_local_temple(
        747841, 0, dict(LIVE_GETDUNGEON), name, origin="cdn"
    )
    backend.observe_capture("/SubmitDungeonLayout", LIVE_UPLOAD, {"accepted": True})
    assert lib.temples[(747841, 0)].layout == name
    assert lib.temples[(747841, 0)].origin == "cdn"


def test_banking_never_breaks_a_live_request(tmp_path: Path) -> None:
    """The player is mid-run; bookkeeping must not cost them the request."""
    backend, _ = _backend(tmp_path)
    for junk in (None, {}, {"dungeonId": 0}, {"dungeonId": 747841}):
        backend.observe_capture("/SubmitDungeonLayout", junk, None)
        backend.observe_capture("/SubmitRun", junk, None)
        backend.observe_capture("/GetDungeon", junk, None)


def test_progression_is_carried_across_without_a_command(tmp_path: Path) -> None:
    """A profile is seeded once and then owned by the offline server.

    Someone who went online afterwards would find their offline account frozen
    at whenever it was first created. Running a command to fix that is a step
    people forget, so the offline server does it on start.
    """
    import json as _json

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "VerifyUserID.jsonl").write_text(
        _json.dumps({
            "ts": "2026-08-27T15:00:00+00:00", "status": 200,
            "request_body": {"userId": 62842},
            "response_body": {
                "userID": 62842, "currentUsername": "Tomb Raider",
                "reputation": 5500, "existingUser": True,
                "currency": {"essence": 1167, "dungeonKeys": [12, 36, 12, 1]},
                "victoryRoutes": [], "playerStats": {},
            },
        }) + "\n", encoding="utf-8")
    # Later that session the player finished a route, so the login is stale.
    (fixtures / "SubmitRun.jsonl").write_text(
        _json.dumps({
            "ts": "2026-08-27T16:32:00+00:00", "status": 200,
            "request_body": {"userId": 62842, "success": 2},
            "response_body": {
                "userID": 62842, "updatedReputation": 5763,
                "currency": {"essence": 1167, "dungeonKeys": [21, 40, 16, 6]},
            },
        }) + "\n", encoding="utf-8")

    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    lib = Library(assets, fixtures)
    backend = OfflineBackend(tmp_path / "state", archive=lib)

    player = backend.profiles.for_player("76561198000000042")
    player.data["userID"] = 62842
    player.data["reputation"] = 364          # frozen at an old value
    player.save()

    carried = backend.sync_from_capture()
    assert carried, "the session should have been carried across"

    fresh = backend.profiles.for_player("76561198000000042").snapshot()
    assert fresh["reputation"] == 5763, "the newest statement wins, not the login"
    assert fresh["currency"]["dungeonKeys"] == [21, 40, 16, 6]


def test_the_same_session_is_not_carried_twice(tmp_path: Path) -> None:
    """Restarting must not keep overwriting offline play with a stale login."""
    import json as _json

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "VerifyUserID.jsonl").write_text(
        _json.dumps({
            "ts": "2026-08-27T15:00:00+00:00", "status": 200,
            "request_body": {"userId": 62842},
            "response_body": {"userID": 62842, "reputation": 5500,
                              "currency": {"essence": 0, "dungeonKeys": [1, 0, 0, 0]}},
        }) + "\n", encoding="utf-8")

    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    backend = OfflineBackend(tmp_path / "state", archive=Library(assets, fixtures))
    player = backend.profiles.for_player("76561198000000042")
    player.data["userID"] = 62842
    player.save()

    assert backend.sync_from_capture(), "first start carries it across"

    # The player then earns something offline.
    player = backend.profiles.for_player("76561198000000042")
    player.data["reputation"] = 6200
    player.save()

    assert backend.sync_from_capture() == [], "nothing newer to carry"
    assert backend.profiles.for_player(
        "76561198000000042"
    ).snapshot()["reputation"] == 6200, "offline progress survived the restart"


def test_another_players_standing_is_never_adopted(tmp_path: Path) -> None:
    """A request we made can return an answer about somebody else.

    /GetDungeonShareList replies with the looked-up player's reputation and
    carries no userID of its own. Trusting the request's userId -- we made it,
    so it is us -- wrote a stranger's standing into this profile: 6421,
    belonging to a player called NP932.
    """
    import json as _json

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "VerifyUserID.jsonl").write_text(
        _json.dumps({
            "ts": "2026-08-27T15:00:00+00:00", "status": 200,
            "request_body": {"userId": 62842},
            "response_body": {"userID": 62842, "reputation": 5763,
                              "currency": {"essence": 1167, "dungeonKeys": [21, 40, 16, 6]}},
        }) + "\n", encoding="utf-8")
    # Looked up later, so newer -- and about a different person entirely.
    (fixtures / "GetDungeonShareList.jsonl").write_text(
        _json.dumps({
            "ts": "2026-08-27T19:00:00+00:00", "status": 200,
            "request_body": {"userId": 62842, "requestedUserName": "NP932"},
            "response_body": {"sharerUserID": 7613, "username": "NP932",
                              "reputation": 6421, "sharedRoutes": []},
        }) + "\n", encoding="utf-8")

    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    lib = Library(assets, fixtures)

    progression = lib.latest_progression(62842)
    assert progression["reputation"] == 5763, "took a stranger's standing"
    assert progression["currency"]["dungeonKeys"] == [21, 40, 16, 6]
