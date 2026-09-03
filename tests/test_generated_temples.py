"""The client can build temples we do not have, and we must keep them.

`UDungeonBuilder` / `UWIBYGameInstance::SendDungeonLayout` show the client
generates a temple locally when the server supplies a seed but no
`layoutDownloadURL`, then uploads it to /SubmitDungeonLayout. That is the only
route to new content once the CDN is gone, so it has to survive a restart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54908"
LAYOUT_DATA = "eyJudW1XaW5ncyI6Miwicm9vbUluZm9zIjpbXX0="  # base64 JSON


def _backend(tmp_path: Path):
    assets = tmp_path / "assets"
    assets.mkdir(exist_ok=True)
    lib = Library(assets, tmp_path / "fixtures")
    return OfflineBackend(tmp_path / "state", archive=lib), lib


def test_unknown_temple_gets_seed_and_no_layout(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path)
    resp = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 5, "dungeonId": 0,
         "dungeonFloorNumber": 0}, BASE
    )
    assert resp["dungeonSeed"] != 0
    assert resp["layoutDownloadURL"] == "", "client must be asked to build it"
    assert resp["dungeonID"] > 0, "needs an id so the upload can be matched"


def test_client_built_layout_is_stored(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    resp = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 5, "dungeonId": 0,
         "dungeonFloorNumber": 0}, BASE
    )
    dungeon_id = resp["dungeonID"]

    out = backend.submit_layout(
        {"dungeonId": dungeon_id, "dungeonFloorNumber": 0,
         "savedLayoutData": LAYOUT_DATA, "areaID": 1}
    )
    assert out["accepted"] is True
    temple = lib.lookup(dungeon_id, 0)
    assert temple is not None and temple.layout


def test_generated_temple_replays_afterwards(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    first = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 5, "dungeonId": 0,
         "dungeonFloorNumber": 0}, BASE
    )
    dungeon_id = first["dungeonID"]
    backend.submit_layout(
        {"dungeonId": dungeon_id, "dungeonFloorNumber": 0,
         "savedLayoutData": LAYOUT_DATA}
    )

    again = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 5, "dungeonId": dungeon_id,
         "dungeonFloorNumber": 0}, BASE
    )
    assert again["dungeonSeed"] == first["dungeonSeed"], "seed must be stable"
    assert again["layoutDownloadURL"].startswith(f"{BASE}/asset/")


def test_generated_temple_survives_restart(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path)
    resp = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 5, "dungeonId": 0,
         "dungeonFloorNumber": 0}, BASE
    )
    dungeon_id = resp["dungeonID"]
    backend.submit_layout(
        {"dungeonId": dungeon_id, "dungeonFloorNumber": 0,
         "savedLayoutData": LAYOUT_DATA}
    )

    reopened = Library(tmp_path / "assets", tmp_path / "fixtures")
    temple = reopened.lookup(dungeon_id, 0)
    assert temple is not None
    assert temple.layout and temple.response


def test_layout_bytes_round_trip(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    resp = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 9, "dungeonId": 0,
         "dungeonFloorNumber": 0}, BASE
    )
    dungeon_id = resp["dungeonID"]
    backend.submit_layout(
        {"dungeonId": dungeon_id, "dungeonFloorNumber": 0,
         "savedLayoutData": LAYOUT_DATA}
    )
    name = lib.lookup(dungeon_id, 0).layout
    assert lib.asset_path(name).read_text("utf-8") == LAYOUT_DATA


def test_submission_without_layout_data_is_safe(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path)
    out = backend.submit_layout({"dungeonId": 123, "dungeonFloorNumber": 0})
    assert out["accepted"] is True


def test_raw_submission_is_recorded(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path)
    backend.submit_layout(
        {"dungeonId": 4242, "dungeonFloorNumber": 1,
         "savedLayoutData": LAYOUT_DATA, "areaID": 3, "dungeonFloorType": 4}
    )
    record = json.loads(
        (tmp_path / "state" / "layouts" / "4242_1.json").read_text("utf-8")
    )
    assert record["areaID"] == 3 and record["dungeonFloorType"] == 4


def test_a_floor_the_client_builds_is_given_no_shape(tmp_path: Path) -> None:
    """The server states a shape only alongside a layout that already has one.

    An earlier version invented one per floor. That was modeled on responses
    which carried a CDN layout, where the shape describes the layout being
    sent -- not on responses where the client is doing the generating. Serving
    an invented shape overrides the client's own logic for that floor.

    Measured across 2,207 captured responses, without exception:

        layout sent -> real layoutType / floorType
        no layout   -> both 0
    """
    backend, _ = _backend(tmp_path)
    for floor in (0, 1, 2):
        resp = backend.get_dungeon(
            {"gameMode": "DGM_ADVENTURE", "routeId": 5, "dungeonId": 0,
             "dungeonFloorNumber": floor}, BASE
        )
        assert not resp["layoutDownloadURL"], f"floor {floor} has no layout"
        assert resp["dungeonLayoutType"] == 0, f"floor {floor}"
        assert resp["dungeonFloorType"] == 0, f"floor {floor}"


def test_a_new_temple_states_no_floor_count(tmp_path: Path) -> None:
    """dungeonFloorTotalCount is 0 until the temple exists (169 captures)."""
    backend, _ = _backend(tmp_path)
    fresh = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 5, "dungeonId": 0,
         "dungeonFloorNumber": 0}, BASE
    )
    assert fresh["dungeonFloorTotalCount"] == 0
    later = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 5,
         "dungeonId": fresh["dungeonID"], "dungeonFloorNumber": 1}, BASE
    )
    assert later["dungeonFloorTotalCount"] == 3, "known once the temple exists"


def test_submit_layout_survives_a_missing_asset_directory(tmp_path: Path) -> None:
    """The handler crashed with FileNotFoundError and returned a stub.

    The client then sat on a black screen waiting for a real submission
    response, so storage problems must never break the reply.
    """
    from phantom_offline.library import Library

    assets = tmp_path / "not-created-yet"
    lib = Library(assets, tmp_path / "fixtures")
    backend = OfflineBackend(tmp_path / "state", archive=lib)
    out = backend.submit_layout(
        {"dungeonId": 4242, "dungeonFloorNumber": 0, "savedLayoutData": LAYOUT_DATA}
    )
    assert out["accepted"] is True
    assert out["dungeonID"] == 4242


def test_a_generated_final_floor_states_no_shape(tmp_path: Path) -> None:
    """Most archived temples lack only their final floor, generated here.

    An earlier version tried to match the shape to the temple's other floors.
    The real server does not do that -- it states no shape whenever it sends no
    layout, and lets the client work the floor out for itself.
    """
    assets = tmp_path / "assets"
    assets.mkdir()
    lib = Library(assets, tmp_path / "fixtures")

    for floor, floor_type in ((0, 1), (1, 4)):
        name = f"local__dungeon-4242-floor-{floor}-layout-test"
        (assets / name).write_text(LAYOUT_DATA, encoding="utf-8")
        lib.record_local_temple(
            4242, floor,
            {"dungeonID": 4242, "dungeonFloorNumber": floor,
             "dungeonLayoutType": 1, "dungeonFloorType": floor_type,
             "dungeonFloorTotalCount": 3, "gameMode": 0, "areaID": 2,
             "dungeonSeed": 111},
            name,
        )

    backend = OfflineBackend(tmp_path / "state", archive=lib)
    final = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 3, "dungeonId": 4242,
         "dungeonFloorNumber": 2}, BASE
    )
    assert not final["layoutDownloadURL"], "final floor should be generated"
    assert final["dungeonLayoutType"] == 0
    assert final["dungeonFloorType"] == 0
    assert final["dungeonFloorTotalCount"] == 3, "the temple's count is known"


def test_archived_floors_keep_their_real_shape(tmp_path: Path) -> None:
    """A replayed layout must still carry the shape that describes it."""
    assets = tmp_path / "assets"
    assets.mkdir()
    lib = Library(assets, tmp_path / "fixtures")
    name = "local__dungeon-777-floor-0-layout-test"
    (assets / name).write_text(LAYOUT_DATA, encoding="utf-8")
    lib.record_local_temple(
        777, 0,
        {"dungeonID": 777, "dungeonFloorNumber": 0, "dungeonLayoutType": 3,
         "dungeonFloorType": 1, "dungeonFloorTotalCount": 3, "gameMode": 1,
         "areaID": 1, "dungeonSeed": 222},
        name,
    )
    backend = OfflineBackend(tmp_path / "state", archive=lib)
    resp = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 3, "dungeonId": 777,
         "dungeonFloorNumber": 0}, BASE
    )
    assert resp["layoutDownloadURL"]
    assert resp["dungeonLayoutType"] == 3
    assert resp["dungeonFloorType"] == 1


def test_an_adventure_request_is_never_served_an_abyss_temple(tmp_path: Path) -> None:
    """Substituting across modes crashes the game on level load.

    Observed live: an archive holding only Abyss temples answered an Adventure
    request with dungeon 248925 -- layoutType 1, two floors, area 2 -- and the
    client died with "Fatal error!" as the level loaded.

    Generating is always available and always safe, so there is no case where
    the wrong mode is better than a fresh temple.
    """
    import json as _json

    assets = tmp_path / "assets"
    assets.mkdir()
    lib = Library(assets, tmp_path / "fixtures")

    # An archive with Abyss temples only, as a fresh test server has.
    for floor, floor_type in ((0, 1), (1, 3)):
        name = f"local__dungeon-248925-floor-{floor}-layout-x"
        (assets / name).write_text(LAYOUT_DATA, encoding="utf-8")
        lib.record_local_temple(
            248925, floor,
            {"dungeonID": 248925, "dungeonFloorNumber": floor, "gameMode": 0,
             "dungeonLayoutType": 1, "dungeonFloorType": floor_type,
             "dungeonFloorTotalCount": 2, "areaID": 2, "dungeonSeed": 679595061},
            name,
        )

    assert lib.any_for_floor(0, game_mode=0) is not None, "Abyss temple is there"
    assert lib.any_for_floor(0, game_mode=1) is None, "must not offer it to Adventure"

    backend = OfflineBackend(tmp_path / "state", archive=lib)
    served = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 0, "dungeonId": 0,
         "dungeonFloorNumber": 0, "playerId": "p"}, BASE
    )
    assert served["gameMode"] == 1, "answered as Adventure"
    assert not served["layoutDownloadURL"], "client builds a fresh one instead"
    assert served["dungeonSeed"] != 0
    assert _json.dumps(served)  # serialisable, nothing exotic leaked through


def test_same_mode_substitution_still_works(tmp_path: Path) -> None:
    """The guard must not stop a temple being reused within its own mode.

    (Abyss itself never reaches the rotation -- its dungeon id comes from the
    route position, so it resolves by id rather than substituting -- which is
    why this is asserted on the library directly.)
    """
    assets = tmp_path / "assets"
    assets.mkdir()
    lib = Library(assets, tmp_path / "fixtures")
    for floor, floor_type in ((0, 1), (1, 4), (2, 3)):
        name = f"local__dungeon-555-floor-{floor}-layout-x"
        (assets / name).write_text(LAYOUT_DATA, encoding="utf-8")
        lib.record_local_temple(
            555, floor,
            {"dungeonID": 555, "dungeonFloorNumber": floor, "gameMode": 1,
             "dungeonLayoutType": 3, "dungeonFloorType": floor_type,
             "dungeonFloorTotalCount": 3, "areaID": 1, "dungeonSeed": 42},
            name,
        )
    offered = lib.any_for_floor(0, game_mode=1)
    assert offered is not None and offered.dungeon_id == 555
    assert lib.any_for_floor(0, game_mode=0) is None, "not to another mode"


def test_an_adventure_run_substitutes_an_adventure_temple(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    lib = Library(assets, tmp_path / "fixtures")
    for floor, floor_type in ((0, 1), (1, 4), (2, 3)):
        name = f"local__dungeon-555-floor-{floor}-layout-x"
        (assets / name).write_text(LAYOUT_DATA, encoding="utf-8")
        lib.record_local_temple(
            555, floor,
            {"dungeonID": 555, "dungeonFloorNumber": floor, "gameMode": 1,
             "dungeonLayoutType": 3, "dungeonFloorType": floor_type,
             "dungeonFloorTotalCount": 3, "areaID": 1, "dungeonSeed": 42},
            name,
        )
    backend = OfflineBackend(tmp_path / "state", archive=lib)
    served = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 0, "dungeonId": 0,
         "dungeonFloorNumber": 0, "playerId": "p"}, BASE
    )
    assert served["layoutDownloadURL"], "an Adventure request may reuse one"
    assert served["gameMode"] == 1


def test_a_generated_temple_can_be_entered_twice(tmp_path: Path) -> None:
    """First entry generates, second replays -- and the second one crashed.

    We correctly state no shape while a temple has no layout. That response was
    then stored, so once the client uploaded what it built we replayed a layout
    alongside zeroes: geometry with nothing describing it, which killed the
    game on level load. Offline-generated temples were single-use.
    """
    backend, lib = _backend(tmp_path)
    req = {"gameMode": "DGM_ADVENTURE", "routeId": 12, "dungeonId": 0,
           "dungeonFloorNumber": 0, "playerId": "p"}

    first = backend.get_dungeon(dict(req), BASE)
    dungeon = first["dungeonID"]
    assert not first["layoutDownloadURL"]
    assert (first["dungeonLayoutType"], first["dungeonFloorType"]) == (0, 0)

    # The client builds it and reports what it made.
    backend.submit_layout({
        "dungeonId": dungeon, "dungeonFloorNumber": 0,
        "savedLayoutData": LAYOUT_DATA,
        "dungeonLayoutType": 3, "dungeonFloorType": 1, "dungeonFloorCount": 3,
        "dungeonSettingsIndex": 0, "gameMode": "DGM_ADVENTURE",
    })

    second = backend.get_dungeon(dict(req, dungeonId=dungeon), BASE)
    assert second["layoutDownloadURL"], "a built temple replays its layout"
    assert second["dungeonLayoutType"] == 3, "and must describe it"
    assert second["dungeonFloorType"] == 1
    assert second["dungeonFloorTotalCount"] == 3


def test_a_layout_is_never_served_without_a_shape(tmp_path: Path) -> None:
    """The rule in both directions: layout <-> shape, one implies the other."""
    backend, _ = _backend(tmp_path)
    for floor, (lt, ft) in enumerate(((3, 1), (3, 4), (2, 3))):
        served = backend.get_dungeon(
            {"gameMode": "DGM_ADVENTURE", "routeId": 77, "dungeonId": 0,
             "dungeonFloorNumber": floor, "playerId": "p"}, BASE
        )
        backend.submit_layout({
            "dungeonId": served["dungeonID"], "dungeonFloorNumber": floor,
            "savedLayoutData": LAYOUT_DATA, "dungeonLayoutType": lt,
            "dungeonFloorType": ft, "dungeonFloorCount": 3,
        })
        again = backend.get_dungeon(
            {"gameMode": "DGM_ADVENTURE", "routeId": 77,
             "dungeonId": served["dungeonID"], "dungeonFloorNumber": floor,
             "playerId": "p"}, BASE
        )
        has_layout = bool(again["layoutDownloadURL"])
        has_shape = again["dungeonLayoutType"] > 0 and again["dungeonFloorType"] > 0
        assert has_layout == has_shape, f"floor {floor}: layout and shape disagree"
