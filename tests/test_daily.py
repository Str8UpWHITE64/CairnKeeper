"""One temple, everybody, until midnight UTC.

Take away "everybody plays the same one" and a daily is an ordinary temple with
a timer on it -- which is what was being served: the rotation counter handed
each request the next temple in the list, so two players on the same day got
different dailies and one player got a different one on every attempt.

There is no seed to derive it from. Every captured GetDailyDungeonInfo from the
real service carries `dungeonSeed: null`; the daily arrives through GetDungeon
like any other temple and the service decides which. So a day not captured
while the service is up is a day nobody can play again, and the days that were
captured have to be replayed in an order everyone agrees on.
"""
from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import daily  # noqa: E402
from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54941"
LAYOUT = base64.b64encode(
    json.dumps({"numWings": 2, "roomInfos": [{"roomIndex": 0}]}).encode()
).decode()


def _archive(tmp_path: Path, dungeons=(747666, 747907, 748001)) -> Library:
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    library = Library(assets, tmp_path / "fx")
    for dungeon in dungeons:
        for floor in range(2):
            blob = f"cdn__dungeon-{dungeon}-floor-{floor}-layout-x"
            (assets / blob).write_text(LAYOUT, encoding="utf-8")
            library.record_local_temple(dungeon, floor, {
                "dungeonID": dungeon, "dungeonFloorNumber": floor,
                "gameMode": 3, "areaID": 1, "dungeonSeed": dungeon + floor,
                "dungeonLayoutType": 3, "dungeonFloorType": 1,
                "dungeonFloorTotalCount": 2,
            }, blob, origin="cdn")
    return Library(assets, tmp_path / "fx")


def _ask(backend: OfflineBackend, player: str) -> int:
    return backend.get_dungeon({
        "playerId": player, "gameMode": "DGM_DAILY", "routeId": 0,
        "routeStage": 0, "dungeonId": 0, "dungeonFloorNumber": 0,
    }, BASE)["dungeonID"]


def test_everybody_gets_the_same_daily(tmp_path: Path) -> None:
    library = _archive(tmp_path)
    backend = OfflineBackend(tmp_path / "state", archive=library)
    got = {_ask(backend, f"pa-player{i}") for i in range(6)}
    assert len(got) == 1, f"six players saw {got}"


def test_the_same_player_asking_again_gets_the_same_one(tmp_path: Path) -> None:
    """Otherwise a daily can be rerolled by quitting to the menu."""
    library = _archive(tmp_path)
    backend = OfflineBackend(tmp_path / "state", archive=library)
    assert len({_ask(backend, "pa-player") for _ in range(5)}) == 1


def test_two_machines_with_the_same_archive_agree(tmp_path: Path) -> None:
    """No service left to be the authority, so the date has to be enough."""
    library = _archive(tmp_path)
    first = OfflineBackend(tmp_path / "one", archive=library)
    second = OfflineBackend(tmp_path / "two", archive=library)
    assert _ask(first, "pa-a") == _ask(second, "pa-b")


def test_a_different_day_is_a_different_daily(tmp_path: Path) -> None:
    library = _archive(tmp_path)
    picks = {
        daily.choose(tmp_path / "state", library, day)
        for day in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
                    "2026-09-05", "2026-09-06")
    }
    assert len(picks) > 1, "every day picking the same temple is not a rotation"


def test_a_day_that_was_captured_gets_its_real_temple(tmp_path: Path) -> None:
    """The recorded answer always beats the invented one."""
    library = _archive(tmp_path)
    daily.record(tmp_path / "state", "2026-09-03", 748001)
    assert daily.choose(tmp_path / "state", library, "2026-09-03") == 748001


def test_a_temple_we_chose_is_never_recorded_as_the_real_one(
    tmp_path: Path,
) -> None:
    """Or the archive starts asserting things it does not know."""
    library = _archive(tmp_path)
    state = tmp_path / "state"
    daily.choose(state, library, "2026-09-03")
    assert daily.recorded(state) == {}


def test_a_recorded_daily_we_no_longer_hold_falls_back(tmp_path: Path) -> None:
    library = _archive(tmp_path)
    daily.record(tmp_path / "state", "2026-09-03", 999999)
    picked = daily.choose(tmp_path / "state", library, "2026-09-03")
    assert picked in (747666, 747907, 748001)


def test_only_complete_daily_temples_are_offered(tmp_path: Path) -> None:
    """A daily that dead-ends on floor two is worse than a substitute."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    library = Library(assets, tmp_path / "fx")
    blob = "cdn__dungeon-747666-floor-0-layout-x"
    (assets / blob).write_text(LAYOUT, encoding="utf-8")
    library.record_local_temple(747666, 0, {
        "dungeonID": 747666, "dungeonFloorNumber": 0, "gameMode": 3,
        "areaID": 1, "dungeonSeed": 5, "dungeonLayoutType": 3,
        "dungeonFloorType": 1, "dungeonFloorTotalCount": 3,
    }, blob, origin="cdn")

    assert daily.candidates(Library(assets, tmp_path / "fx")) == []


def test_an_archive_with_no_dailies_says_so(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir(parents=True)
    library = Library(tmp_path / "assets", tmp_path / "fx")
    assert daily.choose(tmp_path / "state", library) is None


def test_the_window_turns_over_at_midnight_utc(tmp_path: Path) -> None:
    late = datetime(2026, 9, 3, 23, 59, tzinfo=timezone.utc)
    just_after = datetime(2026, 9, 4, 0, 1, tzinfo=timezone.utc)
    assert daily.window(late) == "2026-09-03"
    assert daily.window(just_after) == "2026-09-04"
    assert daily.expires_after("2026-09-03").isoformat().startswith("2026-09-04")


def test_a_failed_capture_records_nothing(tmp_path: Path, monkeypatch) -> None:
    """The first live run of this recorded a day it had never seen.

    The service answered 400 and returned nothing, and the code recorded an
    already-archived temple as that date's real daily -- the archive asserting
    something nobody observed, which is precisely what `record` exists to
    prevent. What arrives has to be told apart from what was already held.
    """
    import phantom_offline.__main__ as cli

    library = _archive(tmp_path)

    class Nothing:
        def __init__(self, *a, **k):
            pass

        def run(self, *, count, on_temple=None):
            return type("Result", (), {"temples": 0})()

    monkeypatch.setattr("phantom_offline.harvest.Harvester", Nothing)
    args = type("Args", (), {"state": tmp_path, "action": "capture",
                             "delay": 0.0})()
    assert cli.cmd_daily(args) == 1
    assert daily.recorded(tmp_path) == {}, "nothing seen, nothing claimed"


def test_a_capture_that_brings_something_new_is_recorded(
    tmp_path: Path, monkeypatch
) -> None:
    import phantom_offline.__main__ as cli

    _archive(tmp_path, dungeons=(747666,))

    class Arrives:
        def __init__(self, state_dir, **k):
            self.state_dir = state_dir

        def run(self, *, count, on_temple=None):
            # Stands in for the harvester writing a temple into the archive.
            library = Library(self.state_dir / "assets", self.state_dir / "fx")
            for floor in range(2):
                blob = f"cdn__dungeon-748500-floor-{floor}-layout-x"
                (self.state_dir / "assets" / blob).write_text(
                    LAYOUT, encoding="utf-8")
                library.record_local_temple(748500, floor, {
                    "dungeonID": 748500, "dungeonFloorNumber": floor,
                    "gameMode": 3, "areaID": 1, "dungeonSeed": 1,
                    "dungeonLayoutType": 3, "dungeonFloorType": 1,
                    "dungeonFloorTotalCount": 2,
                }, blob, origin="cdn")
            return type("Result", (), {"temples": 1})()

    monkeypatch.setattr("phantom_offline.harvest.Harvester", Arrives)
    args = type("Args", (), {"state": tmp_path, "action": "capture",
                             "delay": 0.0})()
    assert cli.cmd_daily(args) == 0
    assert list(daily.recorded(tmp_path).values()) == [748500]
