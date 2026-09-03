"""A player telling a server they do not own that a temple cannot be finished.

This is the only way a shared server ever learns one of its temples is broken:
the operator is not the one playing. It is also the one endpoint that takes
instructions about what to withhold from strangers, so it is the one that has
to assume the caller is lying.

Two checks carry it. They played it -- a report for a temple this server never
handed them is refused, so condemning the archive would mean actually playing
every temple in it. And they are one voice -- a guest's report withholds
nothing until somebody else who ran the same temple agrees.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.blacklist import Blacklist, archived_id  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54931"
LAYOUT = base64.b64encode(
    json.dumps({"numWings": 2, "roomInfos": [{"roomIndex": 0}]}).encode()
).decode()


def _server(tmp_path: Path, floors: int = 3) -> OfflineBackend:
    root = tmp_path / "server"
    (root / "assets").mkdir(parents=True)
    library = Library(root / "assets", root / "fx")
    for floor in range(floors):
        blob = f"pool__dungeon-701683-floor-{floor}-layout-x"
        (root / "assets" / blob).write_text(LAYOUT, encoding="utf-8")
        library.record_local_temple(701683, floor, {
            "dungeonID": 701683, "dungeonFloorNumber": floor, "gameMode": 1,
            "areaID": 1, "dungeonSeed": 5, "dungeonLayoutType": 3,
            "dungeonFloorType": 1, "dungeonFloorTotalCount": floors,
        }, blob, origin="cdn")
    return OfflineBackend(root / "state",
                          archive=Library(root / "assets", root / "fx"))


def _play(backend: OfflineBackend, player: str, floor: int = 0) -> dict:
    return backend.get_dungeon({
        "playerId": player, "gameMode": "DGM_ADVENTURE", "routeId": 0,
        "routeStage": 0, "dungeonId": 0 if floor == 0 else 701683,
        "dungeonFloorNumber": floor,
    }, BASE)


def _report(backend: OfflineBackend, player: str, dungeon: int, floor: int,
            reason: str = "missing-key") -> dict:
    return backend.report_temple({
        "playerId": player, "dungeonId": dungeon, "dungeonFloorNumber": floor,
        "reason": reason,
    })


# ------------------------------------------------------- what is accepted


def test_a_player_can_report_the_temple_they_played(tmp_path: Path) -> None:
    backend = _server(tmp_path)
    served = _play(backend, "pa-guest-1")
    result = _report(backend, "pa-guest-1", served["dungeonID"], 0)

    assert result["accepted"] is True
    assert result["reports"] == 1
    assert result["withheld"] is False, "one voice withholds nothing"
    assert result["needed"] == 1, "and says how many more are needed"


def test_a_floor_played_earlier_in_the_run_can_still_be_reported(
    tmp_path: Path,
) -> None:
    """The floor that stopped you may be a couple of floors back by then."""
    backend = _server(tmp_path)
    _play(backend, "pa-guest-1", floor=0)
    _play(backend, "pa-guest-1", floor=1)
    _play(backend, "pa-guest-1", floor=2)

    assert _report(backend, "pa-guest-1", 701683, 0)["accepted"] is True


def test_two_players_agreeing_withholds_the_temple(tmp_path: Path) -> None:
    backend = _server(tmp_path)
    for player in ("pa-guest-1", "pa-guest-2"):
        _play(backend, player)
    _report(backend, "pa-guest-1", 701683, 0)
    second = _report(backend, "pa-guest-2", 701683, 0)

    assert second["reports"] == 2
    assert second["withheld"] is True
    assert backend.blacklist.blocks_temple(701683, 0) is True


# ------------------------------------------------------- what is refused


def test_you_cannot_report_a_temple_you_never_played(tmp_path: Path) -> None:
    """The check that stops one request in a loop condemning everything."""
    backend = _server(tmp_path)
    result = _report(backend, "pa-nobody", 701683, 0)

    assert result["accepted"] is False
    assert "not served you" in result["reason"]
    assert backend.blacklist.get(archived_id(701683, 0)) is None


def test_you_cannot_report_someone_elses_temple(tmp_path: Path) -> None:
    backend = _server(tmp_path)
    _play(backend, "pa-guest-1")
    assert _report(backend, "pa-guest-2", 701683, 0)["accepted"] is False


def test_pressing_the_button_twice_is_still_one_voice(tmp_path: Path) -> None:
    backend = _server(tmp_path)
    _play(backend, "pa-guest-1")
    for _ in range(6):
        result = _report(backend, "pa-guest-1", 701683, 0)
    assert result["reports"] == 1
    assert result["withheld"] is False


def test_a_made_up_reason_is_not_stored_as_given(tmp_path: Path) -> None:
    """Free text is somebody else's input; the code has to be one of ours."""
    backend = _server(tmp_path)
    _play(backend, "pa-guest-1")
    _report(backend, "pa-guest-1", 701683, 0, reason="<script>everything</script>")
    stored = backend.blacklist.get(archived_id(701683, 0))
    assert stored.reason == "other"


def test_a_long_note_is_cut_down(tmp_path: Path) -> None:
    backend = _server(tmp_path)
    _play(backend, "pa-guest-1")
    backend.report_temple({
        "playerId": "pa-guest-1", "dungeonId": 701683, "dungeonFloorNumber": 0,
        "reason": "missing-key", "note": "x" * 5000,
    })
    assert len(backend.blacklist.get(archived_id(701683, 0)).note) <= 200


# ------------------------------------------------------------ what is kept


def test_the_history_is_short_and_holds_no_identity(tmp_path: Path) -> None:
    """It exists to check a report, not to record what anybody played."""
    backend = _server(tmp_path, floors=3)
    for _ in range(3):
        for floor in range(3):
            _play(backend, "76561198000000042", floor=floor)

    history = backend.state["servedHistory"]
    assert len(history) == 1
    (key, entries), = history.items()
    assert "76561198000000042" not in key, "the platform id is not the key"
    assert len(entries) <= 5
    assert "76561198000000042" not in json.dumps(backend.state)


def test_the_report_reaches_the_server_over_http(tmp_path: Path) -> None:
    """The handler being right is not the same as the route being wired."""
    import threading

    from phantom_offline import server as srv
    from phantom_offline.blacklist import send_report

    root = tmp_path / "server"
    (root / "assets").mkdir(parents=True)
    library = Library(root / "assets", root / "fx")
    blob = "pool__dungeon-701683-floor-0-layout-x"
    (root / "assets" / blob).write_text(LAYOUT, encoding="utf-8")
    library.record_local_temple(701683, 0, {
        "dungeonID": 701683, "dungeonFloorNumber": 0, "gameMode": 1,
        "areaID": 1, "dungeonSeed": 5, "dungeonLayoutType": 3,
        "dungeonFloorType": 1, "dungeonFloorTotalCount": 1,
    }, blob, origin="cdn")

    direct, proxy, _ = srv.build(state_dir=root, direct_port=47971,
                                 proxy_port=47972, mode=srv.MODE_OFFLINE)
    threading.Thread(target=direct.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:47971"
    try:
        # Refused before playing, accepted after -- over the wire both times.
        cold = send_report(base, player_id="76561198000000042",
                           dungeon_id=701683, floor=0, reason="missing-key")
        assert cold["accepted"] is False

        # Played over HTTP, like the report. Both are masked to the same
        # handle on the way in, so the server recognizes them as one player --
        # playing one way and reporting the other looks like two people, which
        # is exactly what the check is meant to catch.
        import urllib.request

        play = urllib.request.Request(
            f"{base}/GetDungeon",
            data=json.dumps({
                "playerId": "76561198000000042", "gameMode": "DGM_ADVENTURE",
                "routeId": 0, "routeStage": 0, "dungeonId": 0,
                "dungeonFloorNumber": 0,
            }).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(play, timeout=15) as answer:
            assert json.loads(answer.read())["dungeonID"] == 701683

        warm = send_report(base, player_id="76561198000000042",
                           dungeon_id=701683, floor=0, reason="missing-key")
        assert warm["accepted"] is True
        assert warm["withheld"] is False
        assert warm["needed"] == 1
    finally:
        direct.server_close()
        proxy.server_close()


def test_an_unreachable_server_is_reported_as_such(tmp_path: Path) -> None:
    """A player pressing the button deserves an answer either way."""
    from phantom_offline.blacklist import send_report

    result = send_report("http://127.0.0.1:1", player_id="x",
                         dungeon_id=1, floor=0, timeout=2.0)
    assert result["accepted"] is False
    assert "could not reach" in result["reason"]
