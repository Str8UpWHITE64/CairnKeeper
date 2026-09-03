"""A temple one player builds must reach the next player, phantoms included.

This is what a self-hosted server is for. Replaying an archive keeps old
content alive; this is how a group makes *new* content for each other once the
service is gone -- one player is handed a seed, their client builds the temple,
and everyone after them gets that same temple with the earlier runs in it.

The chain has four links and every one has to hold:

    seed handed out -> layout uploaded -> run recorded -> both served onward
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54908"
ALICE = "76561198000000111"
BOB = "76561198000000222"
CAROL = "76561198000000333"

LAYOUT = base64.b64encode(
    json.dumps(
        {
            "numWings": 1,
            "roomInfos": [{"roomIndex": i} for i in range(12)],
            "roomsHash": 504573130,
        }
    ).encode()
).decode()
RUN_DATA = base64.b64encode(b"phantom recording bytes" * 400).decode()


def _server(tmp_path: Path) -> tuple[OfflineBackend, Library]:
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    lib = Library(assets, tmp_path / "fx")
    backend = OfflineBackend(tmp_path / "state", archive=lib)
    # Players who have logged in know their own name and standing.
    for player, name, rep in ((ALICE, "Alice", 4210), (BOB, "Bob", 880)):
        profile = backend.profiles.for_player(player)
        profile.data["currentUsername"] = name
        profile.data["reputation"] = rep
        profile._write(profile.data)
    return backend, lib


def _alice_builds_and_runs(backend: OfflineBackend) -> dict:
    """Alice takes a seed, their client builds the temple, they run it."""
    first = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 0, "dungeonId": 0,
         "dungeonFloorNumber": 0, "playerId": ALICE, "whipWagered": "Bamboo"},
        BASE,
    )
    assert not first["layoutDownloadURL"], "a new temple has no layout yet"
    backend.submit_layout(
        {"dungeonId": first["dungeonID"], "dungeonFloorNumber": 0,
         "savedLayoutData": LAYOUT}
    )
    backend.submit_run(
        {"success": 2, "playerId": ALICE, "gameMode": "DGM_ADVENTURE",
         "routeId": first["routeID"], "dungeonId": first["dungeonID"],
         "dungeonFloorNumber": 0, "runData": RUN_DATA,
         "collectedRelicId": "GoldenEgg",
         "currency": {"essence": 0, "dungeonKeys": [1, 0, 0, 0]}}
    )
    return first


def _view(backend: OfflineBackend, player: str, dungeon: int) -> dict:
    return backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 0, "dungeonId": dungeon,
         "dungeonFloorNumber": 0, "playerId": player}, BASE
    )


def test_the_second_player_gets_the_same_temple(tmp_path: Path) -> None:
    backend, _ = _server(tmp_path)
    built = _alice_builds_and_runs(backend)
    seen = _view(backend, BOB, built["dungeonID"])
    assert seen["dungeonSeed"] == built["dungeonSeed"]


def test_the_second_player_is_not_asked_to_rebuild_it(tmp_path: Path) -> None:
    """Rebuilding from the seed could diverge; the stored layout must win."""
    backend, _ = _server(tmp_path)
    built = _alice_builds_and_runs(backend)
    assert _view(backend, BOB, built["dungeonID"])["layoutDownloadURL"]


def test_the_layout_served_is_byte_identical(tmp_path: Path) -> None:
    backend, lib = _server(tmp_path)
    built = _alice_builds_and_runs(backend)
    url = _view(backend, BOB, built["dungeonID"])["layoutDownloadURL"]
    served = lib.asset_path(url.rsplit("/", 1)[-1])
    assert served is not None
    assert served.read_text("utf-8") == LAYOUT


def test_the_second_player_sees_the_first_ones_phantom(tmp_path: Path) -> None:
    backend, _ = _server(tmp_path)
    built = _alice_builds_and_runs(backend)
    ghosts = _view(backend, BOB, built["dungeonID"])["ghostRuns"]
    assert len(ghosts) == 1
    assert ghosts[0]["downloadURL"]


def test_the_phantom_is_named_for_whoever_ran_it(tmp_path: Path) -> None:
    """The client sends no name on /SubmitRun, so it comes from the profile.

    Without that every phantom on a shared server shows one placeholder, and a
    group cannot tell whose ghost they are chasing.
    """
    backend, _ = _server(tmp_path)
    built = _alice_builds_and_runs(backend)
    ghost = _view(backend, BOB, built["dungeonID"])["ghostRuns"][0]
    assert ghost["userName"] == "Alice"
    assert ghost["platformUserID"] == "", "their Steam account is not Bob's business"


def test_the_phantom_carries_the_runners_standing(tmp_path: Path) -> None:
    """`reputation` on /SubmitRun is the route's earnings, not a total.

    Real phantoms from the CDN carry the runner's standing, so using the delta
    would advertise a stranger's whole reputation as one route's worth.
    """
    backend, _ = _server(tmp_path)
    built = _alice_builds_and_runs(backend)
    ghost = _view(backend, BOB, built["dungeonID"])["ghostRuns"][0]
    assert ghost["reputation"] == 4210


def test_a_later_player_sees_it_too(tmp_path: Path) -> None:
    backend, _ = _server(tmp_path)
    built = _alice_builds_and_runs(backend)
    seen = _view(backend, CAROL, built["dungeonID"])
    assert seen["dungeonSeed"] == built["dungeonSeed"]
    assert seen["layoutDownloadURL"]
    assert len(seen["ghostRuns"]) == 1


def test_phantoms_accumulate_as_players_run_it(tmp_path: Path) -> None:
    backend, _ = _server(tmp_path)
    built = _alice_builds_and_runs(backend)
    backend.submit_run(
        {"success": 0, "playerId": BOB, "gameMode": "DGM_ADVENTURE",
         "routeId": 999, "dungeonId": built["dungeonID"],
         "dungeonFloorNumber": 0, "runData": RUN_DATA,
         "currency": {"essence": 0, "dungeonKeys": [0, 0, 0, 0]}}
    )
    ghosts = _view(backend, CAROL, built["dungeonID"])["ghostRuns"]
    assert len(ghosts) == 2
    assert {g["userName"] for g in ghosts} == {"Alice", "Bob"}


def test_the_whole_thing_survives_a_restart(tmp_path: Path) -> None:
    backend, _ = _server(tmp_path)
    built = _alice_builds_and_runs(backend)

    assets = tmp_path / "assets"
    reopened = OfflineBackend(
        tmp_path / "state", archive=Library(assets, tmp_path / "fx")
    )
    seen = _view(reopened, BOB, built["dungeonID"])
    assert seen["dungeonSeed"] == built["dungeonSeed"]
    assert seen["layoutDownloadURL"]
    assert seen["ghostRuns"][0]["userName"] == "Alice"


def test_a_locally_built_temple_reports_its_visitors(tmp_path: Path) -> None:
    """The game says "nobody has been here" while showing you a ghost.

    A temple built on this server has no upstream visitor history, so the
    stored counts are zero -- but we recorded every run on it, so we know.
    """
    backend, _ = _server(tmp_path)
    built = _alice_builds_and_runs(backend)
    seen = _view(backend, BOB, built["dungeonID"])
    assert seen["playerCount"] == 1, "Alice has been here"
    assert seen["deathCount"] == 0, "and they finished it"


def test_deaths_are_counted_separately(tmp_path: Path) -> None:
    backend, _ = _server(tmp_path)
    built = _alice_builds_and_runs(backend)
    backend.submit_run(
        {"success": 0, "playerId": BOB, "gameMode": "DGM_ADVENTURE",
         "routeId": 998, "dungeonId": built["dungeonID"],
         "dungeonFloorNumber": 0, "runData": RUN_DATA,
         "currency": {"essence": 0, "dungeonKeys": [0, 0, 0, 0]}}
    )
    seen = _view(backend, CAROL, built["dungeonID"])
    assert seen["playerCount"] == 2
    assert seen["deathCount"] == 1, "Bob died, Alice did not"


def test_an_archived_temple_keeps_the_real_servers_counts(tmp_path: Path) -> None:
    """Those count every player worldwide, not the ~50 phantoms served.

    One captured response reported 1669 players against 51 phantoms, so
    substituting our phantom count would understate it by thirty-fold.
    """
    import base64 as _b64

    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    lib = Library(assets, tmp_path / "fx")
    name = "local__dungeon-701203-floor-0-layout-real"
    (assets / name).write_text(_b64.b64encode(b"{}").decode(), encoding="utf-8")
    lib.record_local_temple(
        701203, 0,
        {"dungeonID": 701203, "dungeonFloorNumber": 0, "gameMode": 1,
         "dungeonLayoutType": 3, "dungeonFloorType": 1,
         "dungeonFloorTotalCount": 3, "playerCount": 1669, "deathCount": 210,
         "dungeonSeed": 5}, name,
    )
    replayed = lib.temples[(701203, 0)].replay(BASE)
    assert replayed["playerCount"] == 1669
    assert replayed["deathCount"] == 210
