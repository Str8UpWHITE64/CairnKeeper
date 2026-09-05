"""A run finished offline must come back as a phantom.

Once the servers are gone no new phantoms arrive, so the player's own runs are
the only thing keeping an offline temple populated.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54908"

SUBMISSION = {
    "dungeonId": 747351,
    "dungeonFloorNumber": 0,
    "routeId": 222645,
    "userId": 62842,
    "lifetime": 99.46453094482422,
    "success": 1,
    "endLocation": "[146.591370,50962.441406,424.577484]",
    "gameMode": "DGM_CLASSIC",
    "runData": "QkFTRTY0LVBIQU5UT00tUkVDT1JESU5H",
}


def _backend(tmp_path: Path) -> tuple[OfflineBackend, Library]:
    assets = tmp_path / "assets"
    assets.mkdir()
    lib = Library(assets, tmp_path / "fixtures")
    return OfflineBackend(tmp_path / "state", archive=lib), lib


def test_run_is_stored(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    resp = backend.submit_run(dict(SUBMISSION))
    assert resp["dungeonID"] == 747351
    # `storedLocally` was removed for fidelity - the real server sends no such
    # field. Confirm storage through the archive instead.
    assert lib.lookup(747351, 0) is not None


def test_run_becomes_a_phantom(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    backend.submit_run(dict(SUBMISSION))

    temple = lib.lookup(747351, 0)
    assert temple is not None and len(temple.ghosts) == 1

    runs = temple.ghost_runs(BASE)
    assert len(runs) == 1
    assert runs[0]["lifetime"] == SUBMISSION["lifetime"]
    assert runs[0]["downloadURL"].startswith(f"{BASE}/asset/")


def test_phantom_blob_is_servable(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    backend.submit_run(dict(SUBMISSION))
    name = lib.lookup(747351, 0).ghosts[0]
    path = lib.asset_path(name)
    assert path is not None
    assert path.read_text("utf-8") == SUBMISSION["runData"]


def test_survives_restart(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path)
    backend.submit_run(dict(SUBMISSION))

    reopened = Library(tmp_path / "assets", tmp_path / "fixtures")
    temple = reopened.lookup(747351, 0)
    assert temple is not None and len(temple.ghosts) == 1
    assert temple.ghost_runs(BASE)[0]["userName"]


def test_multiple_runs_accumulate(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    backend.submit_run(dict(SUBMISSION))
    backend.submit_run({**SUBMISSION, "lifetime": 42.0})
    assert len(lib.lookup(747351, 0).ghosts) == 2


def test_missing_run_data_is_not_stored(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    backend.submit_run({**SUBMISSION, "runData": ""})
    assert lib.lookup(747351, 0) is None


def test_response_shape_matches_real_server(tmp_path: Path) -> None:
    backend, _ = _backend(tmp_path)
    resp = backend.submit_run(dict(SUBMISSION))
    for field in (
        "attemptID",
        "routeTotalEssence",
        "updatedReputation",
        "activeClassicRoutes",
        "dailySubmissionResponse",
        "maintenanceInfo",
        "userID",
        "routeID",
        "dungeonID",
    ):
        assert field in resp, f"missing {field}"


def test_local_runs_index_is_valid_json(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    backend.submit_run(dict(SUBMISSION))
    entries = json.loads(lib.local_runs_path.read_text("utf-8"))
    assert entries[0]["dungeonID"] == 747351


def test_a_death_is_still_uploaded_as_a_phantom(tmp_path: Path) -> None:
    """Captured traffic shows every run uploads runData, deaths included.

    A death on dungeon 747353 sent 218,696 chars with its endLocation - that is
    how other players see where you fell.
    """
    backend, lib = _backend(tmp_path)
    backend.submit_run(
        {
            **SUBMISSION,
            "success": 0,
            "lifetime": 115.5,
            "endLocation": "[-12735.8,-11048.2,8100.1]",
        }
    )
    temple = lib.lookup(SUBMISSION["dungeonId"], 0)
    assert temple is not None and len(temple.ghosts) == 1

    ghost = temple.ghost_runs(BASE)[0]
    assert ghost["success"] == 0, "the phantom must record that it died"
    assert ghost["endLocation"] == "[-12735.8,-11048.2,8100.1]"
    assert ghost["lifetime"] == 115.5
    assert ghost["downloadURL"].startswith(f"{BASE}/asset/")


def test_ghost_entry_carries_every_field_the_client_expects(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    backend.submit_run(dict(SUBMISSION))
    ghost = lib.lookup(SUBMISSION["dungeonId"], 0).ghost_runs(BASE)[0]
    for field in (
        "runData", "userID", "userName", "success", "lifetime",
        "endLocation", "runID", "downloadURL", "reputation",
        "platform", "platformUserID",
    ):
        assert field in ghost, f"missing {field}"
    assert ghost["runData"] == "", "recording travels by URL, not inline"


def test_every_floor_of_a_run_becomes_its_own_phantom(tmp_path: Path) -> None:
    backend, lib = _backend(tmp_path)
    for floor in (0, 1):
        backend.submit_run({**SUBMISSION, "dungeonFloorNumber": floor, "success": 1})
    assert len(lib.lookup(SUBMISSION["dungeonId"], 0).ghosts) == 1
    assert len(lib.lookup(SUBMISSION["dungeonId"], 1).ghosts) == 1


def test_phantom_carries_the_runner_not_the_server(tmp_path: Path) -> None:
    """Each ghost must show who actually ran it.

    Without this every phantom in a multi-player archive is named "Explorer",
    because the name came from shared server state instead of the submitting
    player's profile.
    """
    backend, lib = _backend(tmp_path)
    alice, bob = "76561198000000001", "76561198000000002"
    backend.profiles.for_player(alice).data["currentUsername"] = "Alice"
    backend.profiles.for_player(bob).data["currentUsername"] = "Bob"

    for player, floor in ((alice, 0), (bob, 1)):
        backend.submit_run(
            {**SUBMISSION, "playerId": player, "dungeonFloorNumber": floor}
        )

    got = {
        lib.lookup(SUBMISSION["dungeonId"], f).ghost_runs(BASE)[0]["userName"]
        for f in (0, 1)
    }
    assert got == {"Alice", "Bob"}


def test_a_phantom_never_carries_its_runners_platform_id(tmp_path: Path) -> None:
    """The field keeps its place; the SteamID64 in it does not.

    A real ghostRuns entry carries the runner's platform account, so the shape
    stays -- but serving the value hands everyone who runs a temple the Steam
    account of everyone else who ran it, and on a shared server it also files
    one on disk for every visitor.
    """
    backend, lib = _backend(tmp_path)
    player = "76561198000000009"
    backend.submit_run({**SUBMISSION, "playerId": player})
    ghost = lib.lookup(SUBMISSION["dungeonId"], 0).ghost_runs(BASE)[0]
    assert "platformUserID" in ghost, "the client expects the field"
    assert ghost["platformUserID"] == ""
    assert player not in json.dumps(ghost)
