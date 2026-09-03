"""Temples reported as unplayable must stop being served.

Phantom Abyss can generate temples that cannot be finished, and once the
servers are gone there is nobody left to fix the generator. An offline server
has to route around them instead: reroll a generated temple's seed, skip an
archived one.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.blacklist import (  # noqa: E402
    IMPORT_THRESHOLD,
    MAX_TEMPLES_PER_REPORTER,
    SOURCE_GUEST,
    STATUS_CLEARED,
    STATUS_CONFIRMED,
    Blacklist,
    Fingerprint,
    archived_id,
)
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54908"
PLAYER = "76561198000000001"

# The shape of the Rift floor that could not be finished: the exit door wanted
# a key that was never placed.
RIFT = Fingerprint(
    gameMode=0, areaID=4, dungeonSettingsIndex=3, dungeonLayoutType=1,
    dungeonFloorType=3, difficultyRating=0, dungeonSeed=1731769676,
)


def _backend(tmp_path: Path) -> OfflineBackend:
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    return OfflineBackend(tmp_path / "state", archive=Library(assets, tmp_path / "fx"))


def _serve(tmp_path: Path, **over) -> tuple[OfflineBackend, dict]:
    """Serve one fixed slot, reopening the server so persistence is exercised."""
    backend = _backend(tmp_path)
    req = {"gameMode": "DGM_ADVENTURE", "routeId": 77, "dungeonId": 0,
           "dungeonFloorNumber": 0, "playerId": PLAYER}
    req.update(over)
    return backend, backend.get_dungeon(req, BASE)


# ------------------------------------------------------------- fingerprints


def test_fingerprint_is_more_than_the_seed(tmp_path: Path) -> None:
    """The same seed in another area builds a different temple.

    Blacklisting on seed alone would withhold temples that are perfectly fine,
    which is the opposite of preserving content.
    """
    elsewhere = Fingerprint(**{**RIFT.__dict__, "areaID": 1})
    assert elsewhere.id != RIFT.id

    other_settings = Fingerprint(**{**RIFT.__dict__, "dungeonSettingsIndex": 0})
    assert other_settings.id != RIFT.id

    other_shape = Fingerprint(**{**RIFT.__dict__, "dungeonFloorType": 1})
    assert other_shape.id != RIFT.id


def test_the_floor_count_is_not_part_of_identity(tmp_path: Path) -> None:
    """The real server reports 0 at mint and the real count afterwards.

    Including it would mean a temple reported when it was minted and requested
    again a moment later fingerprinted as two different temples, so the report
    would silently never take effect.
    """
    response = dict(RIFT.__dict__, dungeonFloorTotalCount=0)
    later = dict(RIFT.__dict__, dungeonFloorTotalCount=2)
    assert Fingerprint.from_response(response) == Fingerprint.from_response(later)


def test_fingerprint_is_stable_across_machines(tmp_path: Path) -> None:
    """Ids must not depend on anything local, or reports are unshareable."""
    assert Fingerprint(**RIFT.__dict__).id == RIFT.id
    assert RIFT.id == Fingerprint(**RIFT.__dict__).id
    assert len(RIFT.id) == 16


def test_fingerprint_reads_a_server_response(tmp_path: Path) -> None:
    response = dict(RIFT.__dict__, routeID=5, ghostRuns=[], playerCount=3)
    assert Fingerprint.from_response(response) == RIFT


# ------------------------------------------------------------------ trust


def test_a_local_report_blocks_immediately(tmp_path: Path) -> None:
    bl = Blacklist(tmp_path / "bl.json")
    assert not bl.blocks_fingerprint(RIFT)
    bl.report_generated(RIFT, reason="missing-key", player_id=PLAYER)
    assert bl.blocks_fingerprint(RIFT)


def test_one_stranger_is_not_enough(tmp_path: Path) -> None:
    """An import must clear a higher bar than something seen on this server."""
    bl = Blacklist(tmp_path / "bl.json")
    bl.import_reports({"reports": [{"id": RIFT.id, "kind": "generated",
                                    "reason": "missing-key",
                                    "fingerprint": RIFT.__dict__, "count": 1}]})
    assert not bl.blocks_fingerprint(RIFT)


def test_agreement_reaches_the_threshold(tmp_path: Path) -> None:
    bl = Blacklist(tmp_path / "bl.json")
    entry = {"id": RIFT.id, "kind": "generated", "reason": "missing-key",
             "fingerprint": RIFT.__dict__, "count": IMPORT_THRESHOLD}
    bl.import_reports({"reports": [entry]})
    assert bl.blocks_fingerprint(RIFT)


def test_confirmed_blocks_regardless_of_count(tmp_path: Path) -> None:
    bl = Blacklist(tmp_path / "bl.json")
    bl.import_reports({"reports": [{"id": RIFT.id, "kind": "generated",
                                    "reason": "missing-key", "count": 1,
                                    "status": STATUS_CONFIRMED,
                                    "fingerprint": RIFT.__dict__}]})
    assert bl.blocks_fingerprint(RIFT)


def test_cleared_overrides_everything(tmp_path: Path) -> None:
    """The escape hatch for a temple reported in error has to be absolute."""
    bl = Blacklist(tmp_path / "bl.json")
    bl.report_generated(RIFT, player_id=PLAYER)
    bl.set_status(RIFT.id, STATUS_CLEARED)
    assert not bl.blocks_fingerprint(RIFT)

    bl.import_reports({"reports": [{"id": RIFT.id, "kind": "generated",
                                    "reason": "missing-key", "count": 99,
                                    "status": STATUS_CONFIRMED,
                                    "fingerprint": RIFT.__dict__}]})
    assert not bl.blocks_fingerprint(RIFT), "an import must not un-clear"


def test_reporting_again_reopens_a_cleared_temple(tmp_path: Path) -> None:
    bl = Blacklist(tmp_path / "bl.json")
    bl.report_generated(RIFT, player_id=PLAYER)
    bl.set_status(RIFT.id, STATUS_CLEARED)
    bl.report_generated(RIFT, reason="softlock", player_id="someone-else")
    assert bl.blocks_fingerprint(RIFT)


def test_the_same_reporter_does_not_count_twice(tmp_path: Path) -> None:
    bl = Blacklist(tmp_path / "bl.json")
    for _ in range(4):
        bl.report_generated(RIFT, player_id=PLAYER)
    assert bl.get(RIFT.id).count == 1


# --------------------------------------------------------------- privacy


def test_an_export_carries_no_player_ids(tmp_path: Path) -> None:
    """Reports get shared; a SteamID64 is a real person's account."""
    bl = Blacklist(tmp_path / "bl.json")
    bl.report_generated(RIFT, reason="missing-key", player_id=PLAYER)
    text = json.dumps(bl.export())
    assert PLAYER not in text
    assert "reporters" not in text
    assert "salt" not in text


def test_pseudonyms_do_not_survive_a_reinstall(tmp_path: Path) -> None:
    """Per-install salting is what stops an export being walked back."""
    a = Blacklist(tmp_path / "a.json")
    b = Blacklist(tmp_path / "b.json")
    assert a.pseudonym(PLAYER) != b.pseudonym(PLAYER)
    assert a.pseudonym(PLAYER) == a.pseudonym(PLAYER)


def test_an_export_round_trips(tmp_path: Path) -> None:
    source = Blacklist(tmp_path / "src.json")
    source.report_generated(RIFT, reason="missing-key", player_id=PLAYER)
    source.report_archived(701203, 2, reason="unreachable-exit", player_id=PLAYER)

    dest = Blacklist(tmp_path / "dst.json")
    added, merged = dest.import_reports(source.export())
    assert (added, merged) == (2, 0)
    assert {r.id for r in dest.reports()} == {RIFT.id, archived_id(701203, 2)}


# ------------------------------------------------------- serving behavior


def test_a_clean_slot_serves_a_stable_seed(tmp_path: Path) -> None:
    _, first = _serve(tmp_path)
    _, second = _serve(tmp_path)
    assert first["dungeonSeed"] == second["dungeonSeed"]


def test_a_reported_temple_is_replaced(tmp_path: Path) -> None:
    backend, first = _serve(tmp_path)
    backend.blacklist.report_generated(
        Fingerprint.from_response(first), reason="missing-key", player_id=PLAYER
    )
    _, replacement = _serve(tmp_path)
    assert replacement["dungeonSeed"] != first["dungeonSeed"]


def test_the_replacement_is_itself_stable(tmp_path: Path) -> None:
    """A rerolled temple must not change under a player mid-run."""
    backend, first = _serve(tmp_path)
    backend.blacklist.report_generated(
        Fingerprint.from_response(first), player_id=PLAYER
    )
    _, second = _serve(tmp_path)
    _, third = _serve(tmp_path)
    assert second["dungeonSeed"] == third["dungeonSeed"]


def test_the_replacement_is_not_itself_blacklisted(tmp_path: Path) -> None:
    backend, first = _serve(tmp_path)
    backend.blacklist.report_generated(
        Fingerprint.from_response(first), player_id=PLAYER
    )
    backend2, replacement = _serve(tmp_path)
    assert not backend2.blacklist.blocks_fingerprint(
        Fingerprint.from_response(replacement)
    )


def test_repeated_reports_keep_finding_new_temples(tmp_path: Path) -> None:
    seen = set()
    for _ in range(4):
        backend, served = _serve(tmp_path)
        assert served["dungeonSeed"] not in seen, "handed out a reported temple"
        seen.add(served["dungeonSeed"])
        backend.blacklist.report_generated(
            Fingerprint.from_response(served), player_id=PLAYER
        )


def test_a_rerolled_temple_still_states_no_shape(tmp_path: Path) -> None:
    """A reroll must not start describing a shape the server would not send."""
    backend, first = _serve(tmp_path)
    backend.blacklist.report_generated(
        Fingerprint.from_response(first), player_id=PLAYER
    )
    _, replacement = _serve(tmp_path)
    assert replacement["dungeonLayoutType"] == 0
    assert replacement["dungeonFloorType"] == 0


def test_abyss_rerolls_keep_the_settings_index(tmp_path: Path) -> None:
    """The reroll must not undo the area's settings collection.

    Serving a Rift floor with area-1 key settings is what made a temple's exit
    door unopenable in the first place, so a reroll that lost the index would
    replace one dead temple with another.
    """
    backend = _backend(tmp_path)

    # The server owns the descent: it derives the stage from the route's own
    # progress rather than trusting whatever the client claims. So reaching
    # the Rift means actually walking down to it.
    route, dungeon = 0, 0
    for _ in range(4):
        served = backend.get_dungeon(
            {"gameMode": "DGM_CLASSIC", "routeId": route, "dungeonId": 0,
             "dungeonFloorNumber": 0, "difficultyRating": 0,
             "playerId": PLAYER}, BASE
        )
        route, dungeon = served["routeID"], served["dungeonID"]
    assert served["areaID"] == 4 and served["dungeonSettingsIndex"] == 3

    backend.blacklist.report_generated(
        Fingerprint.from_response(served), reason="missing-key", player_id=PLAYER
    )
    again = backend.get_dungeon(
        {"gameMode": "DGM_CLASSIC", "routeId": route, "dungeonId": dungeon,
         "dungeonFloorNumber": 0, "difficultyRating": 0,
         "playerId": PLAYER}, BASE
    )
    assert again["dungeonSeed"] != served["dungeonSeed"], "reroll failed"
    assert again["dungeonSettingsIndex"] == 3
    assert again["areaID"] == 4
    assert (again["dungeonLayoutType"], again["dungeonFloorType"]) == (0, 0)


# ------------------------------------------------------ reporting from play


def test_the_last_served_temple_is_recorded(tmp_path: Path) -> None:
    """A player mid-game cannot read a dungeon id off the screen."""
    backend, served = _serve(tmp_path)
    last = backend.last_served(PLAYER)
    assert last is not None
    assert last["dungeonSeed"] == served["dungeonSeed"]
    assert Fingerprint.from_response(last) == Fingerprint.from_response(served)


def test_last_served_is_per_player(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    for player, route in (("p1", 1), ("p2", 2)):
        backend.get_dungeon(
            {"gameMode": "DGM_ADVENTURE", "routeId": route, "dungeonId": 0,
             "dungeonFloorNumber": 0, "playerId": player}, BASE
        )
    assert backend.last_served("p1")["routeID"] == 1
    assert backend.last_served("p2")["routeID"] == 2


def test_last_served_survives_a_restart(tmp_path: Path) -> None:
    """Reporting happens after the player has closed the game."""
    _, served = _serve(tmp_path)
    reopened = _backend(tmp_path)
    assert reopened.last_served(PLAYER)["dungeonSeed"] == served["dungeonSeed"]


# ------------------------------------------------------------ resilience


def test_a_corrupt_blacklist_does_not_stop_the_server(tmp_path: Path) -> None:
    """Withholding content is a nicety; serving temples is the job."""
    path = tmp_path / "bl.json"
    path.write_text("{ this is not json", encoding="utf-8")
    bl = Blacklist(path)
    assert bl.reports() == []
    assert not bl.blocks_fingerprint(RIFT)
    assert path.with_suffix(".corrupt").exists(), "keep the bad file to inspect"


def test_an_unknown_report_id_is_handled(tmp_path: Path) -> None:
    bl = Blacklist(tmp_path / "bl.json")
    assert bl.set_status("nope", STATUS_CONFIRMED) is None
    assert bl.remove("nope") is False


def test_a_reported_temple_is_not_replayed_from_storage(tmp_path: Path) -> None:
    """The case the reroll exists for: a temple the client already built.

    Once the client uploads a layout it is stored and looked up by dungeon id
    on re-entry, which would hand the player back the very temple they just
    reported as unfinishable.
    """
    backend = _backend(tmp_path)
    req = {"gameMode": "DGM_ADVENTURE", "routeId": 31, "dungeonId": 0,
           "dungeonFloorNumber": 0, "playerId": PLAYER}
    first = backend.get_dungeon(dict(req), BASE)
    dungeon_id = first["dungeonID"]

    # The client builds it and uploads what it built.
    backend.submit_layout({
        "dungeonId": dungeon_id, "dungeonFloorNumber": 0,
        "savedLayoutData": "eyJudW1XaW5ncyI6Miwicm9vbUluZm9zIjpbXX0=",
    })
    replayed = backend.get_dungeon(dict(req, dungeonId=dungeon_id), BASE)
    assert replayed["layoutDownloadURL"], "stored temple should replay"

    backend.blacklist.report_generated(
        Fingerprint.from_response(first), reason="missing-key", player_id=PLAYER
    )

    after = backend.get_dungeon(dict(req, dungeonId=dungeon_id), BASE)
    assert after["dungeonSeed"] != first["dungeonSeed"], "reroll was bypassed"
    assert not after["layoutDownloadURL"], "must rebuild, not replay the bad one"


# ------------------------------------------------- reports from other people


def test_a_guests_report_alone_withholds_nothing(tmp_path: Path) -> None:
    """One player must not be able to condemn a temple for everybody.

    On a server other people connect to, a report arriving over the wire is
    somebody else's word about a temple the operator never played. It is
    recorded and shown, and it withholds nothing until a second player who ran
    the same temple agrees.
    """
    black = Blacklist(tmp_path / "blacklist.json")
    report = black.report_archived(701683, 0, reason="missing-key",
                                   player_id="guest-1", source=SOURCE_GUEST)
    assert report.count == 1
    assert report.blocks() is False
    assert black.blocks_temple(701683, 0) is False


def test_a_second_guest_agreeing_is_enough(tmp_path: Path) -> None:
    black = Blacklist(tmp_path / "blacklist.json")
    black.report_archived(701683, 0, reason="missing-key",
                          player_id="guest-1", source=SOURCE_GUEST)
    black.report_archived(701683, 0, reason="missing-key",
                          player_id="guest-2", source=SOURCE_GUEST)
    assert black.blocks_temple(701683, 0) is True


def test_the_same_guest_twice_is_still_one_voice(tmp_path: Path) -> None:
    """Otherwise the threshold is met by pressing the button twice."""
    black = Blacklist(tmp_path / "blacklist.json")
    for _ in range(5):
        black.report_archived(701683, 0, reason="missing-key",
                              player_id="guest-1", source=SOURCE_GUEST)
    assert black.get(archived_id(701683, 0)).count == 1
    assert black.blocks_temple(701683, 0) is False


def test_the_operators_own_report_still_acts_at_once(tmp_path: Path) -> None:
    """It is their machine and they were there."""
    black = Blacklist(tmp_path / "blacklist.json")
    report = black.report_archived(701683, 0, reason="missing-key",
                                   player_id="me")
    assert report.blocks() is True
    assert black.blocks_temple(701683, 0) is True


def test_a_player_who_reports_everything_stops_counting(tmp_path: Path) -> None:
    """The abuse this is actually for.

    Somebody reporting every temple they are served would, with a threshold
    alone, only need one friend to blacklist the whole archive between them.
    Past a point their word stops counting toward the threshold -- while their
    reports stay on record, so an operator can look and decide for themselves.
    """
    black = Blacklist(tmp_path / "blacklist.json")
    for dungeon in range(MAX_TEMPLES_PER_REPORTER + 5):
        black.report_archived(700000 + dungeon, 0, reason="other",
                              player_id="spammer", source=SOURCE_GUEST)
    # An honest player agrees about exactly one of them.
    black.report_archived(700003, 0, reason="missing-key",
                          player_id="honest", source=SOURCE_GUEST)

    assert "spammer" not in {r for r in black.get(
        archived_id(700003, 0)).reporters or []}, "their vote is discounted"
    assert black.blocks_temple(700003, 0) is False, "one honest voice is not two"
    assert black.blocks_temple(700001, 0) is False, "and the rest stay playable"

    # The record is kept, so an operator can still see what was claimed.
    stored = json.loads((tmp_path / "blacklist.json").read_text("utf-8"))
    every = [r for r in stored["reports"].values()]
    assert any(len(r.get("reporters") or []) for r in every)


def test_an_operator_can_still_confirm_a_discounted_report(tmp_path: Path) -> None:
    """Discounting a voice must not stop a human overruling it."""
    black = Blacklist(tmp_path / "blacklist.json")
    for dungeon in range(MAX_TEMPLES_PER_REPORTER + 2):
        black.report_archived(700000 + dungeon, 0, reason="other",
                              player_id="spammer", source=SOURCE_GUEST)
    assert black.blocks_temple(700001, 0) is False
    black.set_status(archived_id(700001, 0), STATUS_CONFIRMED)
    assert black.blocks_temple(700001, 0) is True
