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
