"""Abyss routes must descend all four areas offline.

An Abyss route is four dungeons, one per area, and the client asks for the next
one by requesting dungeonId 0 on a route it already holds. The live server
refuses that without a submitted run in between; offline we are the server, so
a player can descend the whole route with nothing fabricated.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend, floor_shape  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54908"
PLAYER = "76561198000000001"

# Floor totals the game advertises on the Abyss difficulty screen.
MASTER, NIGHTMARE = 0, 2
EXPECTED_FLOORS = {MASTER: 8, NIGHTMARE: 12}


def _backend(tmp_path: Path) -> OfflineBackend:
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    return OfflineBackend(tmp_path / "state", archive=Library(assets, tmp_path / "fx"))


def _walk(backend: OfflineBackend, difficulty: int) -> dict[tuple[int, int], dict]:
    """Descend a whole Abyss route the way the client does."""
    out: dict[tuple[int, int], dict] = {}
    route = 0
    for _ in range(4):
        first = backend.get_dungeon(
            {"gameMode": "DGM_CLASSIC", "routeId": route, "dungeonId": 0,
             "dungeonFloorNumber": 0, "difficultyRating": difficulty,
             "playerId": PLAYER}, BASE
        )
        route = first["routeID"]
        dungeon = first["dungeonID"]
        out[(dungeon, 0)] = first
        # A newly minted temple reports dungeonFloorTotalCount 0 -- the real
        # server does not state a count until the temple exists. The count
        # arrives with the next floor.
        probe = backend.get_dungeon(
            {"gameMode": "DGM_CLASSIC", "routeId": route, "dungeonId": dungeon,
             "dungeonFloorNumber": 1, "difficultyRating": difficulty,
             "playerId": PLAYER}, BASE
        )
        out[(dungeon, 1)] = probe
        for floor in range(2, probe["dungeonFloorTotalCount"]):
            out[(dungeon, floor)] = backend.get_dungeon(
                {"gameMode": "DGM_CLASSIC", "routeId": route, "dungeonId": dungeon,
                 "dungeonFloorNumber": floor, "difficultyRating": difficulty,
                 "playerId": PLAYER}, BASE
            )
    return out


def test_master_route_has_eight_floors(tmp_path: Path) -> None:
    assert len(_walk(_backend(tmp_path), MASTER)) == EXPECTED_FLOORS[MASTER]


def test_nightmare_route_has_twelve_floors(tmp_path: Path) -> None:
    assert len(_walk(_backend(tmp_path), NIGHTMARE)) == EXPECTED_FLOORS[NIGHTMARE]


def test_route_descends_all_four_areas(tmp_path: Path) -> None:
    """Before this worked, every request returned the same area-1 dungeon."""
    floors = _walk(_backend(tmp_path), MASTER)
    assert sorted({f["areaID"] for f in floors.values()}) == [1, 2, 3, 4]


def test_each_area_is_a_different_dungeon(tmp_path: Path) -> None:
    floors = _walk(_backend(tmp_path), MASTER)
    assert len({dungeon for dungeon, _ in floors}) == 4


def test_every_floor_gets_its_own_seed(tmp_path: Path) -> None:
    """The seed key was built before the area was resolved, so all four
    first floors hashed from dungeonId 0 and shared one seed -- the client
    would have built the same temple in Ruins, Caverns, Inferno and Rift.
    """
    for difficulty in (MASTER, NIGHTMARE):
        floors = _walk(_backend(tmp_path / str(difficulty)), difficulty)
        seeds = [f["dungeonSeed"] for f in floors.values()]
        collisions = [s for s, n in Counter(seeds).items() if n > 1]
        assert not collisions, f"difficulty {difficulty} reused seeds"


def test_abyss_floor_shapes_match_captured_temples(tmp_path: Path) -> None:
    """Abyss uses layoutType 1 throughout; floorType is positional.

    Adventure's shapes (layoutType 3/2) would make the generator emit an empty
    temple and hang the client on a black screen.
    """
    assert floor_shape(0, total=2, abyss=True) == (1, 1)
    assert floor_shape(1, total=2, abyss=True) == (1, 3)
    assert floor_shape(0, total=3, abyss=True) == (1, 1)
    assert floor_shape(1, total=3, abyss=True) == (1, 4)
    assert floor_shape(2, total=3, abyss=True) == (1, 3)


def test_a_generated_floor_describes_no_shape(tmp_path: Path) -> None:
    """The real server states a shape only when it also sends a layout.

    Measured across 2,207 captured responses with no exceptions. Inventing one
    overrides the client's own logic for a floor it is about to build, which is
    how an Abyss floor got an exit door whose key was never placed.
    """
    for floor in _walk(_backend(tmp_path), MASTER).values():
        assert not floor["layoutDownloadURL"], "no layout in this fixture"
        assert floor["dungeonLayoutType"] == 0
        assert floor["dungeonFloorType"] == 0


def test_route_progress_survives_a_restart(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    first = backend.get_dungeon(
        {"gameMode": "DGM_CLASSIC", "routeId": 0, "dungeonId": 0,
         "dungeonFloorNumber": 0, "playerId": PLAYER}, BASE
    )
    route = first["routeID"]

    reopened = _backend(tmp_path)
    second = reopened.get_dungeon(
        {"gameMode": "DGM_CLASSIC", "routeId": route, "dungeonId": 0,
         "dungeonFloorNumber": 0, "playerId": PLAYER}, BASE
    )
    assert second["areaID"] == 2, "a restart must not reset the descent"


def test_adventure_is_unaffected(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    resp = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 0, "dungeonId": 0,
         "dungeonFloorNumber": 0, "playerId": PLAYER}, BASE
    )
    # Brand new temple: no count stated yet, and no shape.
    assert resp["dungeonFloorTotalCount"] == 0
    assert resp["dungeonLayoutType"] == 0


def test_settings_index_tracks_the_area(tmp_path: Path) -> None:
    """Abyss settingsIndex == routeStage: 0/1/2/3 for the four areas.

    Confirmed across 380 dungeons in other players' shared routes. The index
    selects the dungeon settings collection, which carries TempleKeyChances --
    so sending 0 everywhere generated later areas with first-area key settings
    and produced temples whose exit door could not be opened.
    """
    floors = _walk(_backend(tmp_path), MASTER)
    by_area = {f["areaID"]: f["dungeonSettingsIndex"] for f in floors.values()}
    assert by_area == {1: 0, 2: 1, 3: 2, 4: 3}


def test_adventure_settings_index_is_untouched(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    resp = backend.get_dungeon(
        {"gameMode": "DGM_ADVENTURE", "routeId": 0, "dungeonId": 0,
         "dungeonFloorNumber": 0, "dungeonSettingsIndex": 0,
         "playerId": PLAYER}, BASE
    )
    assert resp["dungeonSettingsIndex"] == 0


def test_route_totals_match_what_the_menu_advertises(tmp_path: Path) -> None:
    """Floors per area are not uniform across a route.

    Unchained advertises 13 floors over four areas, which no single per-area
    number produces. An earlier model stored one count per difficulty and made
    Unchained 16 and Insane 12; Master and Nightmare only worked because 8 and
    12 happen to divide evenly by four.
    """
    from phantom_offline.backend import abyss_floors

    advertised = {MASTER: 8, 1: 10, NIGHTMARE: 12, 3: 13}
    for difficulty, total in advertised.items():
        walked = sum(abyss_floors(difficulty, area) for area in range(4))
        assert walked == total, f"difficulty {difficulty}"


def test_unchained_areas_are_three_three_three_four(tmp_path: Path) -> None:
    """Areas 1-3 were observed at 3 floors each; the 4th follows from 13."""
    from phantom_offline.backend import abyss_floors

    assert [abyss_floors(3, a) for a in range(4)] == [3, 3, 3, 4]


def test_unchained_route_serves_thirteen_floors(tmp_path: Path) -> None:
    assert len(_walk(_backend(tmp_path), 3)) == 13


def test_the_deepest_area_can_be_longer_than_the_rest(tmp_path: Path) -> None:
    """The Rift is the area that differs, so it must not inherit area 1."""
    floors = _walk(_backend(tmp_path), 3)
    by_area = {}
    for f in floors.values():
        by_area.setdefault(f["areaID"], set()).add(f["dungeonFloorNumber"])
    assert len(by_area[4]) == 4, "the Rift is four floors on Unchained"
    assert len(by_area[1]) == 3


def _stocked(tmp_path: Path, areas=(1, 2, 3, 4), floors=2, ghosts=3):
    """A backend whose archive holds one complete Abyss temple per area."""
    import base64

    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    lib = Library(assets, tmp_path / "fx")
    layout = base64.b64encode(b'{"numWings":1,"roomInfos":[]}').decode()
    for area in areas:
        dungeon = 700000 + area
        for floor in range(floors):
            name = f"local__dungeon-{dungeon}-floor-{floor}-layout-x"
            (assets / name).write_text(layout, encoding="utf-8")
            lib.record_local_temple(
                dungeon, floor,
                {"dungeonID": dungeon, "dungeonFloorNumber": floor, "gameMode": 0,
                 "areaID": area, "dungeonLayoutType": 1,
                 "dungeonFloorType": 1 if floor == 0 else 3,
                 "dungeonFloorTotalCount": floors, "dungeonSeed": 1000 + dungeon,
                 "routeID": 999999, "routeStage": 0, "playerCount": 40},
                name,
            )
            for g in range(ghosts):
                ghost = f"local__dungeon-{dungeon}-floor-{floor}-runinfo-{g}"
                (assets / ghost).write_text("x", encoding="utf-8")
                lib.record_local_run(dungeon, floor, ghost, {"userName": f"runner{g}"})
    return OfflineBackend(tmp_path / "state", archive=lib), lib


def _descend(backend, difficulty=MASTER, player=PLAYER):
    out = []
    route = 0
    for _ in range(4):
        first = backend.get_dungeon(
            {"gameMode": "DGM_CLASSIC", "routeId": route, "dungeonId": 0,
             "dungeonFloorNumber": 0, "difficultyRating": difficulty,
             "playerId": player}, BASE
        )
        route = first["routeID"]
        out.append(first)
    return out


def test_abyss_adopts_archived_temples_instead_of_generating(tmp_path: Path) -> None:
    """Every offline Abyss run used to be empty by construction.

    Abyss takes its dungeon id from the route position rather than asking for
    one, so the substitution path -- which only runs for dungeonId 0 -- never
    fired, and thousands of archived phantoms went unused.
    """
    backend, _ = _stocked(tmp_path)
    served = _descend(backend)
    assert all(f["layoutDownloadURL"] for f in served), "all four areas archived"
    assert sum(len(f["ghostRuns"]) for f in served) > 0


def test_a_replayed_temple_carries_our_route_not_its_own(tmp_path: Path) -> None:
    """A captured response still names the route it was originally served on.

    The client reads routeID back to us on the next request, so replaying the
    archive's verbatim handed our bookkeeping a stranger's route -- the stage
    stopped advancing and the player revisited an area.
    """
    backend, _ = _stocked(tmp_path)
    served = _descend(backend)
    assert [f["areaID"] for f in served] == [1, 2, 3, 4]
    assert all(f["routeID"] != 999999 for f in served), "the archive's route leaked"
    assert [f["routeStage"] for f in served] == [0, 1, 2, 3]
    assert [f["dungeonSettingsIndex"] for f in served] == [0, 1, 2, 3]


def test_a_temple_already_beaten_is_not_served_again(tmp_path: Path) -> None:
    """Phantoms are the scarce thing, but only to someone who has not seen them."""
    backend, _ = _stocked(tmp_path)
    first_pass = _descend(backend)
    beaten = {f["dungeonID"] for f in first_pass}

    profile = backend.profiles.for_player(PLAYER)
    profile.data["victoryRoutes"] = [
        {"routeID": 1, "gameMode": 0,
         "dungeons": [{"dungeonID": d, "attemptedStatus": 2}]}
        for d in beaten
    ]
    profile._write(profile.data)

    second_pass = _descend(backend)
    assert not (beaten & {f["dungeonID"] for f in second_pass}), "reserved a beaten temple"
    assert all(not f["layoutDownloadURL"] for f in second_pass), "nothing left to reuse"


def test_the_floor_count_must_match_the_difficulty(tmp_path: Path) -> None:
    """Nightmare is 3 floors an area; a 2-floor temple leaves the exit missing."""
    backend, _ = _stocked(tmp_path, floors=2)
    served = _descend(backend, difficulty=NIGHTMARE)
    assert all(not f["layoutDownloadURL"] for f in served), \
        "a 2-floor temple must not fill a 3-floor slot"
    assert [f["areaID"] for f in served] == [1, 2, 3, 4]


def test_a_substituted_temple_matches_the_difficulty_asked_for(tmp_path) -> None:
    """The real server never answered a difficulty it was not asked for.

    Measured across 2,428 captured exchanges, with no exceptions: asked 0 got
    0, asked 60 got 60. On a fresh run it maps straight onto the area -- 0 to
    area 1, 20 to 2, 40 to 3, 60 to 4.

    Substituting across it handed somebody starting a fresh run an area 4
    temple built for difficulty 60. The phantoms walked through the walls and
    the game crashed two floors later, which is the same failure as answering
    an Adventure request with an Abyss temple, one notch less obvious.
    """
    import base64
    import json

    (tmp_path / "assets").mkdir(parents=True)
    library = Library(tmp_path / "assets", tmp_path / "fixtures")

    for dungeon, difficulty, area in ((701001, 0, 1), (701142, 60, 4)):
        blob = f"pool__dungeon-{dungeon}-floor-0-layout-x"
        (tmp_path / "assets" / blob).write_text(
            base64.b64encode(json.dumps(
                {"numWings": 2, "roomInfos": [{"roomIndex": 0}]}).encode()
            ).decode(), encoding="utf-8")
        library.record_local_temple(dungeon, 0, {
            "dungeonID": dungeon, "dungeonFloorNumber": 0, "gameMode": 1,
            "areaID": area, "dungeonSeed": 5, "dungeonLayoutType": 3,
            "dungeonFloorType": 1, "dungeonFloorTotalCount": 1,
            "difficultyRating": difficulty,
        }, blob, origin="cdn")

    easy = library.any_for_floor(0, game_mode=1, difficulty=0)
    assert easy is not None and easy.dungeon_id == 701001

    hard = library.any_for_floor(0, game_mode=1, difficulty=60)
    assert hard is not None and hard.dungeon_id == 701142

    # Nothing at this difficulty. Serving one of the others is the bug; the
    # caller hands out a seed instead and the client builds its own.
    assert library.any_for_floor(0, game_mode=1, difficulty=20) is None
