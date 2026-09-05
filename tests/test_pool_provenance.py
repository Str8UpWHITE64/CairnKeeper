"""A shared pool must never accept a temple this server invented.

Offline dungeon ids are drawn from the same range the live service uses -- it
has reached 747,840 and keeps climbing, while ours spread from 100,000 to
1,000,000, so roughly four in five land inside the occupied band. Nothing in an
id distinguishes them.

Uploaded to a pool keyed on that id, a synthetic temple is indistinguishable
from a real one carrying the same number, and one silently displaces the other.
It also has nothing to offer: anyone can generate their own.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54908"
PLAYER = "76561198000000001"
LAYOUT = base64.b64encode(b'{"numWings":1,"roomInfos":[]}').decode()


def _backend(tmp_path: Path):
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    lib = Library(assets, tmp_path / "fx")
    return OfflineBackend(tmp_path / "state", archive=lib), lib


def _build_offline(backend: OfflineBackend) -> int:
    """A temple generated entirely by our own server."""
    first = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 4, "dungeonId": 0,
         "dungeonFloorNumber": 0, "playerId": PLAYER}, BASE
    )
    backend.submit_layout(
        {"dungeonId": first["dungeonID"], "dungeonFloorNumber": 0,
         "savedLayoutData": LAYOUT, "dungeonLayoutType": 3,
         "dungeonFloorType": 1, "dungeonFloorCount": 3}
    )
    return first["dungeonID"]


def test_a_temple_we_minted_is_marked_offline(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    dungeon = _build_offline(backend)
    assert lib.temples[(dungeon, 0)].origin == "client-offline"


def test_a_temple_the_live_service_minted_is_poolable(tmp_path: Path) -> None:
    """During a capture session the id comes from upstream, so it is real.

    The client still builds the temple, but the service assigned the id and
    other players can be served that same temple -- it is genuine content.
    """
    backend, lib = _backend(tmp_path)
    backend.submit_layout(
        {"dungeonId": 747841, "dungeonFloorNumber": 0, "savedLayoutData": LAYOUT,
         "dungeonLayoutType": 3, "dungeonFloorType": 1, "dungeonFloorCount": 3}
    )
    assert lib.temples[(747841, 0)].origin == "client-online"


def test_the_pool_excludes_what_we_generated(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    mine = _build_offline(backend)
    backend.submit_layout(
        {"dungeonId": 747841, "dungeonFloorNumber": 0, "savedLayoutData": LAYOUT,
         "dungeonLayoutType": 3, "dungeonFloorType": 1, "dungeonFloorCount": 3}
    )
    poolable = lib.poolable()
    assert (747841, 0) in poolable, "a real temple belongs in the pool"
    assert (mine, 0) not in poolable, "ours does not"


def test_a_synthetic_id_can_collide_with_a_real_one(tmp_path: Path) -> None:
    """The failure this guards against, made concrete.

    Two temples, same id, different provenance. Only the real one may travel.
    """
    backend, lib = _backend(tmp_path)
    mine = _build_offline(backend)
    # Somebody else's archive holds a genuine temple on that very id.
    assert 100000 <= mine <= 1000000, "our ids sit in the live service's range"
    assert lib.temples[(mine, 0)].origin == "client-offline"
    assert (mine, 0) not in lib.poolable()


def test_provenance_survives_a_restart(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path)
    mine = _build_offline(backend)
    reopened = Library(tmp_path / "assets", tmp_path / "fx")
    assert reopened.temples[(mine, 0)].origin == "client-offline"
    assert (mine, 0) not in reopened.poolable()


def test_records_written_before_provenance_are_treated_as_ours(tmp_path: Path) -> None:
    """Withholding a real temple costs one upload; the reverse corrupts a pool."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    lib = Library(assets, tmp_path / "fx")
    name = "local__dungeon-555-floor-0-layout-x"
    (assets / name).write_text(LAYOUT, encoding="utf-8")
    lib.record_local_temple(555, 0, {"dungeonID": 555, "gameMode": 1}, name)

    import json
    path = assets / "local_temples.json"
    entries = json.loads(path.read_text("utf-8"))
    for entry in entries:
        entry.pop("origin", None)          # as an older build would have left it
    path.write_text(json.dumps(entries), encoding="utf-8")

    reopened = Library(assets, tmp_path / "fx")
    assert reopened.temples[(555, 0)].origin == "client-offline"
    assert (555, 0) not in reopened.poolable()


def test_a_generated_layout_never_displaces_an_archived_one(tmp_path) -> None:
    """The phantoms were recorded running through the archived building.

    When the server has no layout it hands over a seed and the client builds
    its own, which is then kept so the temple survives. But keeping it must not
    mean serving it in place of the real one that arrives later or was there
    all along: the client would build a different building and every phantom on
    that floor would walk through its walls.

    Found by playing. Floors 0 and 1 of a temple served a generated layout
    while floor 2 still had the genuine one; the phantoms went through the
    walls on both, and the game crashed on the third floor.
    """
    import base64
    import json

    from phantom_offline.library import Library, is_generated_layout

    (tmp_path / "assets").mkdir(parents=True)
    payload = base64.b64encode(json.dumps(
        {"numWings": 2, "roomInfos": [{"roomIndex": 0}]}).encode()).decode()
    archived = "version_128__2024-02-23__dungeon-701142-floor-0-layout-olgrt5ca"
    generated = "local__dungeon-701142-floor-0-layout-generated"
    for name in (archived, generated):
        (tmp_path / "assets" / name).write_text(payload, encoding="utf-8")

    assert is_generated_layout(generated)
    assert not is_generated_layout(archived)

    library = Library(tmp_path / "assets", tmp_path / "fixtures")
    library.record_local_temple(701142, 0, {
        "dungeonID": 701142, "dungeonFloorNumber": 0, "gameMode": 1,
        "areaID": 4, "dungeonSeed": 214985010, "dungeonFloorTotalCount": 3,
    }, generated, origin="client-offline")

    again = Library(tmp_path / "assets", tmp_path / "fixtures")
    kept = again.temples[(701142, 0)]
    assert kept.layout == archived, "the archived building, not the invented one"
    assert kept.origin == "cdn", "and it is still archive provenance"


def test_a_generated_layout_is_kept_when_it_is_all_there_is(tmp_path) -> None:
    """The other half. Without this the temple is lost entirely."""
    import base64
    import json

    from phantom_offline.library import Library

    (tmp_path / "assets").mkdir(parents=True)
    generated = "local__dungeon-702000-floor-0-layout-generated"
    (tmp_path / "assets" / generated).write_text(base64.b64encode(json.dumps(
        {"numWings": 2, "roomInfos": [{"roomIndex": 0}]}).encode()).decode(),
        encoding="utf-8")

    library = Library(tmp_path / "assets", tmp_path / "fixtures")
    library.record_local_temple(702000, 0, {
        "dungeonID": 702000, "dungeonFloorNumber": 0, "gameMode": 1,
        "areaID": 1, "dungeonSeed": 5, "dungeonFloorTotalCount": 1,
    }, generated, origin="client-offline")

    again = Library(tmp_path / "assets", tmp_path / "fixtures")
    assert again.temples[(702000, 0)].layout == generated
