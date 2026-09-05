"""A divergence report is meant to be shared, so it must carry nothing personal.

Replaying captured exchanges against the offline backend is how this project
finds its own bugs -- six real ones in a single afternoon, four of which a green
test suite was hiding. Running it on players' machines reaches corners of the
protocol this project never will.

That only works if the report is safe to send. The first version was not: it
walked dictionary keys as though they were field names, and in several maps the
keys are player names and SteamIDs. It would have shipped a 427-person social
graph in a file described as anonymous.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import fidelity  # noqa: E402
from phantom_offline.fidelity import Divergence, Report  # noqa: E402


def _walk(real, ours):
    found: list[Divergence] = []
    fidelity._walk(real, ours, "", found, "VerifyUserID")
    return found


def test_a_map_keyed_by_player_names_is_never_opened(tmp_path: Path) -> None:
    """The keys here are people, not fields."""
    real = {"playerStats": {"NumTimesRunWithPlayer": {"Alyssiya": 2, "AFKV": 1}}}
    ours = {"playerStats": {"NumTimesRunWithPlayer": {"Alyssiya": 3}}}
    text = json.dumps([d.__dict__ for d in _walk(real, ours)])
    assert "Alyssiya" not in text
    assert "AFKV" not in text


def test_a_steam_id_used_as_a_key_does_not_escape(tmp_path: Path) -> None:
    real = {"playerStats": {"PlatformsOfPlayersRunWith": {"76561198000000777": 1}}}
    ours = {"playerStats": {"PlatformsOfPlayersRunWith": {}}}
    text = json.dumps([d.__dict__ for d in _walk(real, ours)])
    assert "76561198000000777" not in text


def test_a_new_data_keyed_stat_is_covered_by_default(tmp_path: Path) -> None:
    """Naming known maps individually means the next one leaks.

    Everything under playerStats is opaque, so a stat added later is safe
    without anyone remembering to list it.
    """
    real = {"playerStats": {"SomeStatInventedLater": {"a-persons-name": 1}}}
    ours = {"playerStats": {"SomeStatInventedLater": {}}}
    text = json.dumps([d.__dict__ for d in _walk(real, ours)])
    assert "a-persons-name" not in text


def test_the_export_drops_anything_not_shaped_like_a_field(tmp_path: Path) -> None:
    """Second defense, in case the walk ever lets something through."""
    report = Report(checked=1)
    report.divergences = [
        Divergence("VerifyUserID", "playerStats.Ran.Some Player Name", "extra"),
        Divergence("VerifyUserID", "playerStats.Ran.76561198000000777", "extra"),
        Divergence("GetDungeon", "dungeonLayoutType", "type", real="0", ours="1"),
    ]
    fields = [d["field"] for d in report.export()["divergences"]]
    assert fields == ["dungeonLayoutType"]


def test_a_real_divergence_still_gets_through(tmp_path: Path) -> None:
    """Safety that reports nothing would be no safer than reporting nothing."""
    real = {"dungeonLayoutType": 0, "dungeonFloorType": 0}
    ours = {"dungeonLayoutType": 1, "dungeonFloorType": 0}
    found = _walk(real, ours)
    assert len(found) == 1
    assert found[0].field_path == "dungeonLayoutType"
    assert (found[0].real, found[0].ours) == ("0", "1")


def test_small_numbers_are_named_because_that_is_the_bug(tmp_path: Path) -> None:
    """A layoutType of 1 where the real server sends 0 is the whole point."""
    assert fidelity._shape(0) == "0"
    assert fidelity._shape(3) == "3"


def test_large_numbers_are_not_disclosed(tmp_path: Path) -> None:
    """Seeds and ids are numbers too."""
    assert fidelity._shape(-951959203) == "int"
    assert fidelity._shape(747879, "dungeonID") == "int"


def test_strings_are_never_disclosed(tmp_path: Path) -> None:
    assert fidelity._shape("Tomb Raider") == "string"
    assert fidelity._shape("") == "empty-string"


def test_volatile_fields_are_not_reported(tmp_path: Path) -> None:
    """Seeds and timestamps differ every time and mean nothing when they do."""
    real = {"dungeonSeed": 1, "maintenanceTimeUTC": "a"}
    ours = {"dungeonSeed": 2, "maintenanceTimeUTC": "b"}
    assert _walk(real, ours) == []


def test_an_exported_report_is_plain_readable_json(tmp_path: Path) -> None:
    """A player should be able to read what they are about to send."""
    report = Report(checked=3)
    report.divergences = [Divergence("GetDungeon", "areaID", "type", "1", "2", 5)]
    out = json.dumps(report.export(), indent=2)
    assert "areaID" in out
    assert json.loads(out)["divergences"][0]["count"] == 5


def test_a_login_matches_the_real_one_structurally(tmp_path) -> None:
    """Field for field and type for type, all the way down.

    The client reads the login into a struct. A nested struct that will not
    convert takes the whole reply with it, and the client is left holding
    defaults -- which is what an offline one was found holding: logged in and
    verified by its own account, user id 0, no routes, no whips.

    Three of these were wrong at once. lastRouteInfo had six fields against
    fourteen, with `sandbag` for `sandBag` and strings where numbers went.
    playerStats echoed the client's own spelling back, so JustBeatArea went
    out as "Ruins" where the server sends a number, and the floor timers kept
    the client's casing one level down. dailyDungeonInfo carried a seed the
    real one never sent and omitted two fields it always did.
    """
    from phantom_offline.backend import OfflineBackend

    backend = OfflineBackend(tmp_path / "state")
    answer = backend.verify_user({
        "playerId": "76561198000000042", "currentUsername": "Tomb Raider",
        "platform": "STEAM", "userId": 0,
    })

    route = answer["lastRouteInfo"]
    assert set(route) == {
        "routeID", "gameMode", "shareCode", "completedByID", "completedByName",
        "routeAttemptCount", "routeAttemptID", "dungeonVersion", "curseLevel",
        "wageredWhipID", "purchased", "sandBag", "storedCurrency", "dungeons",
    }
    assert route["completedByID"] == 0, "a number, not an empty string"
    assert route["wageredWhipID"] is None, "null, not -1"
    assert route["storedCurrency"] is None, "null, not 0"
    assert "sandbag" not in route, "the real one spells it sandBag"

    daily = answer["dailyDungeonInfo"]
    assert set(daily) == {"expiryTime", "leaderboardType", "leaderboard",
                          "clearanceRate", "routeID"}
    assert "dungeonSeed" not in daily, "the seed belongs in the temple answer"


def test_stats_go_back_in_the_types_the_server_used(tmp_path) -> None:
    """The client's own spelling and representation are not the server's."""
    from phantom_offline.profile import STAT_TYPES, as_server_stats

    out = as_server_stats({
        "JustBeatArea": "Ruins",            # the client says where; the server says how many
        "JustClaimedRelic": "SoldierDart",
        "FloorTimers": [{"areaIndex": 0, "floorIndex": 1, "floorTime": 12.5}],
        "SomethingInvented": 1,
    })
    assert out["JustBeatArea"] == 0, "a number, whatever the client called it"
    assert out["JustClaimedRelic"] == "SoldierDart", "and strings stay strings"
    assert out["FloorTimers"] == [{"AreaIndex": 0, "FloorIndex": 1,
                                   "FloorTime": 12.5}]
    assert "SomethingInvented" not in out
    assert set(out) == set(STAT_TYPES), "every field it sends, and only those"


def test_existing_user_says_whether_this_server_knows_them(tmp_path) -> None:
    """It reports the server's record, not what the client remembered.

    The captures made it look like it mirrored the request -- 24 logins where
    a client sending userId 0 got false and one sending an id got true. That
    correlation is real but incidental: live, a client that had no id was a
    player the server had never seen either. Offline the two come apart,
    because a profile can have a history the client's own save does not.

    Answering false to a returning player sends them through the tutorial and
    throws their routes away. Found by doing exactly that.
    """
    from phantom_offline.backend import OfflineBackend

    backend = OfflineBackend(tmp_path / "state")
    player = "76561198000000042"

    first = backend.verify_user({"playerId": player, "userId": 0,
                                 "currentUsername": "Tomb Raider"})
    assert first["existingUser"] is False, "nobody has seen them yet"

    backend.submit_run({
        "playerId": player, "routeId": 7, "dungeonId": 900,
        "dungeonFloorNumber": 1, "success": 2,
        "currency": {"essence": 0, "dungeonKeys": [1, 0, 0, 0]},
    })

    # Same client, still no id of its own, but now with a history here.
    again = backend.verify_user({"playerId": player, "userId": 0,
                                 "currentUsername": "Tomb Raider"})
    assert again["existingUser"] is True, "this server has met them since"
    assert again["victoryRoutes"], "and their route is theirs to get back"


def test_times_are_written_the_way_the_server_wrote_them(tmp_path) -> None:
    """`2026-08-26T00:00:00Z`, not `20260826T000000Z`.

    The compact form came from a `%Y%m%dT%H%M%S%sZ` pattern found in the
    binary. That is a pattern the client parses with, not one the server ever
    answered in: every timestamp in every captured reply carries the dashes
    and the colons.

    A datetime the client cannot parse fails the struct it is being read into,
    and a failed struct takes the whole login with it. That is the entire
    reason an offline player was logged in, verified, and holding no user id,
    no routes, no whips and no collection -- and it was the expiry stamp on a
    daily dungeon nobody was playing.
    """
    import re

    from phantom_offline.backend import OfflineBackend

    backend = OfflineBackend(tmp_path / "state")
    answer = backend.verify_user({"playerId": "76561198000000042", "userId": 0,
                                  "currentUsername": "Tomb Raider"})
    shape = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

    stamps: list[tuple[str, str]] = []

    def gather(value, path=""):
        if isinstance(value, str) and "T" in value and value[:2] == "20":
            stamps.append((path, value))
        elif isinstance(value, dict):
            for k, v in value.items():
                gather(v, f"{path}.{k}" if path else k)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                gather(v, f"{path}[{i}]")

    gather(answer)
    assert stamps, "the login carries at least one timestamp"
    for where, value in stamps:
        assert shape.match(value), f"{where} is {value!r}, which will not parse"
