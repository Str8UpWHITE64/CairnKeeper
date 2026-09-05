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


def test_a_banked_route_looks_like_one_the_real_server_banked(tmp_path: Path) -> None:
    """Four things were wrong at once, and the client died on the fourth.

    Measured across 55 real victory routes with no exceptions: a dungeon
    carries the id of the route it belongs to, attemptedStatus is 2, the route
    names who finished it, and storedCurrency is null once the route has ended.

    Ours banked routeID 0, attemptedStatus -1, completedByID 0 and the
    currency the run was still holding. On the next launch the game played the
    relic cutscene and then died in a stack overflow two frames deep -- a
    dungeon pointing at no route at all being the likeliest thing to walk in
    a circle.
    """
    backend = _backend(tmp_path)
    backend.submit_run(
        {
            "playerId": PLAYER,
            "routeId": 4242,
            "dungeonId": 701683,
            "dungeonFloorNumber": 2,
            "success": 2,
            "collectedRelicId": "SoldierDart",
            "currency": {"essence": 9, "dungeonKeys": [5, 0, 0, 0]},
        }
    )
    route = backend.profiles.for_player(PLAYER).snapshot()["victoryRoutes"][0]

    assert route["storedCurrency"] is None, "the route has ended"
    assert route["completedByID"], "somebody finished it"
    for dungeon in route["dungeons"]:
        assert dungeon["routeID"] == route["routeID"], (
            "a dungeon naming no route is what the client walked in circles on")
        assert dungeon["attemptedStatus"] == 2, "it was completed"
        if dungeon.get("relicIDs"):
            assert dungeon["relicCollectorUserIDs"] not in ([0], [None], None), (
                "somebody collected the relic")


def test_the_route_handed_back_is_not_the_route_kept(tmp_path: Path) -> None:
    """They are different shapes, and a live capture is what settled it.

    Handed back in lastRunRouteInfo, straight after the run: dungeons[].routeID
    is 0, attemptedStatus is -1, storedCurrency holds what the run was
    carrying. Read back out of victoryRoutes at the next login the same route
    names its own id, says 2, and stores no currency.

    Banking the returned shape leaves a dungeon pointing at no route at all,
    and the client played the relic cutscene on the next launch and then died
    in a stack overflow. Normalising both the same way is how that happened:
    one measurement, taken from victoryRoutes, applied to a function that also
    feeds the reply.
    """
    backend = _backend(tmp_path)
    answer = backend.submit_run(
        {
            "playerId": PLAYER,
            "routeId": 205655,
            "dungeonId": 701296,
            "dungeonFloorNumber": 2,
            "success": 2,
            "collectedRelicId": "MarbleRelic",
            "currency": {"essence": 0, "dungeonKeys": [6, 0, 0, 0]},
        }
    )

    handed_back = answer["lastRunRouteInfo"]
    assert handed_back["storedCurrency"] == {"essence": 0,
                                             "dungeonKeys": [6, 0, 0, 0]}
    assert handed_back["dungeons"][0]["routeID"] == 0
    assert handed_back["dungeons"][0]["attemptedStatus"] == -1

    kept = backend.profiles.for_player(PLAYER).snapshot()["victoryRoutes"][0]
    assert kept["storedCurrency"] is None
    assert kept["dungeons"][0]["routeID"] == kept["routeID"] == 205655
    assert kept["dungeons"][0]["attemptedStatus"] == 2

    # True of both, and the one thing the old code got wrong in both places.
    for shape in (handed_back, kept):
        assert shape["completedByID"], "somebody finished it"
        assert shape["dungeons"][0]["relicCollectorUserIDs"] not in ([0], None)


def test_no_login_ever_carries_a_null_inside_an_array(tmp_path: Path) -> None:
    """The one that cost two days.

    `relicCollectorNames` went out as `[null]` on any route that collected a
    relic. The real server sent plain `null` there in all 1,092 dungeon
    records it was ever captured sending, relic or not -- and a null inside an
    array of strings is not something the client can read.

    One of them, in one route, lost the whole login. The client reported
    itself logged in, verified, server responsive, save matching -- and held
    no user id, no routes, no whips and no collection, while the run counters
    in its own save ticked up normally. Handed a captured login instead, the
    same client took the id immediately.

    Written as a sweep rather than a field check because the shape is the
    problem, not the field.
    """
    backend = _backend(tmp_path)
    backend.submit_run({
        "playerId": PLAYER, "routeId": 4242, "dungeonId": 701683,
        "dungeonFloorNumber": 2, "success": 2, "collectedRelicId": "SoldierDart",
        "currency": {"essence": 9, "dungeonKeys": [5, 0, 0, 0]},
    })
    answer = backend.verify_user({"playerId": PLAYER, "userId": 0,
                                  "currentUsername": "Tomb Raider"})

    found: list[str] = []

    def sweep(value, path=""):
        if isinstance(value, list):
            if any(v is None for v in value):
                found.append(path)
            for i, v in enumerate(value):
                sweep(v, f"{path}[{i}]")
        elif isinstance(value, dict):
            for k, v in value.items():
                sweep(v, f"{path}.{k}" if path else k)

    sweep(answer)
    assert not found, f"a null sits inside an array at {found}"
    assert answer["victoryRoutes"], "and the route is still there"


def test_routes_stored_with_the_old_shape_are_corrected_on_the_way_out(
    tmp_path: Path,
) -> None:
    """Profiles already hold routes written before anyone knew better.

    Corrected as they are spoken rather than by rewriting them: what is stored
    is a record of a run that happened.
    """
    p = Profile(tmp_path / "p.json")
    p.record_victory({
        "routeID": 1, "wageredWhipID": "Bamboo",
        "dungeons": [{"dungeonID": 100, "relicIDs": ["Panda"],
                      "relicCollectorNames": [None]}],
    })
    assert p.snapshot()["victoryRoutes"][0]["dungeons"][0][
        "relicCollectorNames"] == [None], "stored as it was written"

    from phantom_offline.backend import _as_sent_route

    spoken = _as_sent_route(p.snapshot()["victoryRoutes"][0])
    assert spoken["dungeons"][0]["relicCollectorNames"] is None


def test_the_reply_carries_a_list_and_the_history_carries_null(tmp_path: Path) -> None:
    """relicCollectorNames, the third field on this struct to be two shapes.

    In a run submission the real server sends a list of names, one per relic:
    [null] in 56 of 57 captured replies, an actual name in the other. Read back
    out of victoryRoutes at the next login it is plain null, in all 1,092
    records.

    Both directions cost something. A null inside an array of strings is not
    readable, so a list in the login loses the whole reply -- the client keeps
    no id, no routes, no collection. Null where the reply wants a list loses
    the run's own answer, which is what grants the relic as the player walks
    back into the hub.
    """
    backend = _backend(tmp_path)
    answer = backend.submit_run({
        "playerId": PLAYER, "routeId": 17, "dungeonId": 701296,
        "dungeonFloorNumber": 2, "success": 2, "collectedRelicId": "MarbleRelic",
        "currency": {"essence": 0, "dungeonKeys": [1, 0, 0, 0]},
    })

    handed_back = answer["lastRunRouteInfo"]["dungeons"][0]
    assert handed_back["relicCollectorNames"] == [None], "a list, one per relic"

    login = backend.verify_user({"playerId": PLAYER, "userId": 0,
                                 "currentUsername": "Tomb Raider"})
    kept = login["victoryRoutes"][0]["dungeons"][0]
    assert kept["relicCollectorNames"] is None, "and null once it is history"


def test_a_route_names_the_player_it_belongs_to(tmp_path: Path) -> None:
    """A player's in-game id can move; their history should follow it.

    The id is allocated here rather than issued to them, so it changes when
    the allocation does. A route left naming an id they no longer have is one
    the client does not count as theirs -- the login carried both routes and
    the relic and the whip stayed locked anyway.

    A zero means a relic somebody else collected, and stays zero.
    """
    from phantom_offline.backend import _as_sent_route

    route = {
        "routeID": 14,
        "completedByID": 841573782,
        "dungeons": [{
            "dungeonID": 701683,
            "relicIDs": ["SoldierDart", "Panda"],
            "relicCollectorUserIDs": [841573782, 0],
            "relicCollectorNames": [None],
        }],
    }
    sent = _as_sent_route(route, 820948)
    assert sent["completedByID"] == 820948
    assert sent["dungeons"][0]["relicCollectorUserIDs"] == [820948, 0], (
        "theirs follows them; somebody else's zero stays a zero")
    assert sent["dungeons"][0]["relicCollectorNames"] is None

    # Nothing is invented when there is no id to speak for.
    untouched = _as_sent_route(route, None)
    assert untouched["completedByID"] == 841573782
