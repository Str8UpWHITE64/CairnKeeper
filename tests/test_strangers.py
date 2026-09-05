"""Somebody downloads the client and connects to a server they do not own.

The whole point of a backup server is that other people can use it. They arrive
with the game and an address: no archive, no profile, no captured login. What
has to be true for them:

* they get an account of their own, not the host's
* they run alongside phantoms real players left behind
* their own run is kept, under their own name
* the next player runs alongside them

Every one of these was wrong the first time it was simulated. The host's entire
save -- 58 purchases, 59 victory routes, 5,763 reputation -- was handed to the
first stranger who connected, every player after that got a blank account the
client could not start a run with, and runs were filed under the host's name.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54921"

LAYOUT = base64.b64encode(
    json.dumps({"numWings": 2, "roomInfos": [{"roomIndex": 0}]}).encode()
).decode()

# A captured login for the host, and one from an account that had never played.
HOST_LOGIN = {
    "currentUsername": "Tomb Raider", "userID": 62842, "existingUser": True,
    "reputation": 5763, "currency": {"essence": 1167, "dungeonKeys": [27, 45, 26, 13]},
    "permanentPurchases": ["BigChestsDetails:Dungeon:141766:0"],
    "victoryRoutes": [{"routeID": 114265}],
    "sharerID": "<player-id>", "platformVerificationID": "<redacted>",
    "upgrades": {"upgradeLevels": [4, 22] + [0] * 23},
}
FIRST_LOGIN = {
    "currentUsername": "Second Account", "userID": 404886, "existingUser": False,
    "reputation": 0, "currency": {"essence": 0, "dungeonKeys": None},
    "permanentPurchases": [], "victoryRoutes": [],
    "sharerID": "<player-id>", "platformVerificationID": "<redacted>",
    "upgrades": {"upgradeLevels": [0] * 25},
}


def _server(tmp_path: Path) -> OfflineBackend:
    """An operator's server: pooled temples, captured logins, no credentials."""
    root = tmp_path / "server"
    (root / "assets").mkdir(parents=True)
    (root / "fixtures").mkdir(parents=True)
    (root / "fixtures" / "VerifyUserID.jsonl").write_text(
        "\n".join(json.dumps({"status": 200, "response_body": body})
                  for body in (FIRST_LOGIN, HOST_LOGIN)),
        encoding="utf-8")

    library = Library(root / "assets", root / "fixtures")
    for floor in range(2):
        blob = f"pool__dungeon-701683-floor-{floor}-layout-x"
        (root / "assets" / blob).write_text(LAYOUT, encoding="utf-8")
        library.record_local_temple(701683, floor, {
            "dungeonID": 701683, "dungeonFloorNumber": floor, "gameMode": 1,
            "areaID": 1, "dungeonSeed": 5, "dungeonLayoutType": 3,
            "dungeonFloorType": 1, "dungeonFloorTotalCount": 2,
        }, blob, origin="cdn")
        ghost = f"pool__dungeon-701683-floor-{floor}-runinfo-a"
        (root / "assets" / ghost).write_text("recording", encoding="utf-8")
        library.record_local_run(701683, floor, ghost,
                                 {"userName": "Shrim", "userID": 322356,
                                  "runID": 900, "success": 1})

    library = Library(root / "assets", root / "fixtures")
    return OfflineBackend(root / "state", archive=library)


def _sign_in(backend: OfflineBackend, player: str, name: str) -> dict:
    return backend.verify_user({
        "userId": 0, "playerId": player, "platform": "STEAM",
        "currentUsername": name, "verificationToken": "steam-ticket",
        "platformVerificationId": "", "clientDungeonVersion": 128,
        "clientProtocolVersion": 7,
    })


def test_a_stranger_does_not_inherit_the_hosts_save(tmp_path: Path) -> None:
    backend = _server(tmp_path)
    alice = _sign_in(backend, "76561198000000111", "Alice")

    assert alice["currentUsername"] == "Alice"
    assert alice["permanentPurchases"] == []
    assert alice["victoryRoutes"] == []
    assert alice["reputation"] == 0
    assert alice["currency"] == {"essence": 0, "dungeonKeys": None}
    assert alice["existingUser"] is False, "a true fresh start"
    assert alice["userID"] != 62842


def test_arriving_first_earns_nothing(tmp_path: Path) -> None:
    """The old rule gave the capture to whoever connected first."""
    backend = _server(tmp_path)
    _sign_in(backend, "76561198000000111", "Alice")
    host = _sign_in(backend, "76561198000000042", "Tomb Raider")

    assert host["reputation"] == 5763, "the host's save is still the host's"
    assert host["userID"] == 62842, "and so is their in-game id"
    assert len(host["permanentPurchases"]) == 1


def test_nothing_of_the_hosts_reaches_a_stranger(tmp_path: Path) -> None:
    backend = _server(tmp_path)
    everything = json.dumps(_sign_in(backend, "76561198000000222", "Bob"))
    for leak in ("Tomb Raider", "62842", "Second Account", "404886",
                 "<redacted>", "<player-id>"):
        assert leak not in everything, leak


def test_two_strangers_do_not_share_an_account(tmp_path: Path) -> None:
    backend = _server(tmp_path)
    alice = _sign_in(backend, "76561198000000111", "Alice")
    bob = _sign_in(backend, "76561198000000222", "Bob")
    assert alice["userID"] != bob["userID"]
    assert alice["currentUsername"] != bob["currentUsername"]


def test_a_stranger_runs_a_real_temple_and_is_kept(tmp_path: Path) -> None:
    """The round trip the server exists for."""
    backend = _server(tmp_path)
    _sign_in(backend, "76561198000000111", "Alice")

    served = backend.get_dungeon({
        "playerId": "76561198000000111", "gameMode": "DGM_ADVENTURE",
        "routeId": 0, "routeStage": 0, "dungeonId": 0, "dungeonFloorNumber": 0,
    }, BASE)
    assert served["dungeonID"] == 701683
    assert [g["userName"] for g in served["ghostRuns"]] == ["Shrim"]

    backend.submit_run({
        "playerId": "76561198000000111", "dungeonId": 701683,
        "dungeonFloorNumber": 0, "gameMode": "DGM_ADVENTURE",
        "success": 0, "lifetime": 88.5, "runData": "alice-recording",
    })

    _sign_in(backend, "76561198000000222", "Bob")
    again = backend.get_dungeon({
        "playerId": "76561198000000222", "gameMode": "DGM_ADVENTURE",
        "routeId": 0, "routeStage": 0, "dungeonId": 701683,
        "dungeonFloorNumber": 0,
    }, BASE)

    names = [g["userName"] for g in again["ghostRuns"]]
    assert "Alice" in names, "their run must be there for the next player"
    assert "Shrim" in names, "and so must the ones that were already there"

    actors = [f"Ghost{g['userID']}" for g in again["ghostRuns"]]
    assert len(set(actors)) == len(actors), "or the client dies on spawn"


def test_a_second_profile_starts_empty(tmp_path: Path) -> None:
    """One player, two profiles, one captured login.

    The capture was matched by display name, and both profiles sign in under
    the same name -- so the second one was handed the first one's login. The
    profile it merged over was blank, but everything a blank profile does not
    override came through: relics, locked routes, the last route played. A
    fresh profile arrived already finished, which is the opposite of the
    feature.

    The capture belongs to the profile that inherited it, and to no other.
    """
    backend = _server(tmp_path)
    main = _sign_in(backend, "pa-first", "Tomb Raider")
    assert main["reputation"] == 5763, "the profile that owns the capture"

    second = _sign_in(backend, "pa-second", "Tomb Raider")
    assert second["reputation"] == 0
    assert second["permanentPurchases"] == []
    assert second["victoryRoutes"] == []
    assert second["existingUser"] is False
    assert second["currency"] == {"essence": 0, "dungeonKeys": None}
    assert not second.get("relics")
    assert second["userID"] != main["userID"]


def test_going_back_to_the_first_profile_finds_it_whole(tmp_path: Path) -> None:
    backend = _server(tmp_path)
    _sign_in(backend, "pa-first", "Tomb Raider")
    _sign_in(backend, "pa-second", "Tomb Raider")
    again = _sign_in(backend, "pa-first", "Tomb Raider")
    assert again["reputation"] == 5763
    assert len(again["permanentPurchases"]) == 1
