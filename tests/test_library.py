"""The archive must index real CDN blobs and replay them safely."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.library import BLOB_RE, Library, Temple  # noqa: E402

LAYOUT = "version_128__2024-02-23__dungeon-701203-floor-2-layout-ygck77l0"
GHOST = "version_128__2026-04-27__dungeon-701203-floor-2-runinfo-lttxkd70"

REAL_RESPONSE = {
    "dungeonID": 701203,
    "dungeonFloorNumber": 2,
    "dungeonSeed": -951959203,
    "dungeonLayoutType": 2,
    "dungeonFloorType": 3,
    "layoutDownloadURL": "https://dungeons.wiby.net/version_128/2024-02-23/"
    "dungeon-701203-floor-2-layout-ygck77l0",
    "ghostRuns": [
        {
            "userName": "TH3 P1ON33R",
            "lifetime": 235.164,
            "success": 2,
            "runData": "",
            "downloadURL": "https://dungeons.wiby.net/version_128/2026-04-27/"
            "dungeon-701203-floor-2-runinfo-lttxkd70",
        }
    ],
}


def _make_archive(tmp_path: Path) -> tuple[Path, Path]:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / LAYOUT).write_bytes(b"layout-bytes")
    (assets / GHOST).write_bytes(b"ghost-bytes")

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "GetDungeon.jsonl").write_text(
        json.dumps({"status": 200, "response_body": REAL_RESPONSE}) + "\n",
        encoding="utf-8",
    )
    return assets, fixtures


def test_blob_name_parsing() -> None:
    m = BLOB_RE.search(LAYOUT)
    assert m and m["dungeon"] == "701203" and m["floor"] == "2"
    assert m["kind"] == "layout"
    assert BLOB_RE.search(GHOST)["kind"] == "runinfo"


def test_indexes_layout_and_ghosts(tmp_path: Path) -> None:
    lib = Library(*_make_archive(tmp_path))
    temple = lib.lookup(701203, 2)
    assert temple is not None
    assert temple.layout == LAYOUT
    assert temple.ghosts == [GHOST]


def test_replay_preserves_real_seed(tmp_path: Path) -> None:
    lib = Library(*_make_archive(tmp_path))
    out = lib.lookup(701203, 2).replay("http://127.0.0.1:54908")
    assert out["dungeonSeed"] == -951959203
    assert out["dungeonLayoutType"] == 2
    assert out["dungeonFloorType"] == 3


def test_replay_rewrites_cdn_urls_to_local(tmp_path: Path) -> None:
    lib = Library(*_make_archive(tmp_path))
    out = lib.lookup(701203, 2).replay("http://127.0.0.1:54908")
    dumped = json.dumps(out)
    assert "dungeons.wiby.net" not in dumped, "offline mode must not reach the CDN"
    assert out["layoutDownloadURL"].startswith("http://127.0.0.1:54908/asset/")
    assert out["ghostRuns"][0]["downloadURL"].startswith("http://127.0.0.1:54908/asset/")


def test_replay_keeps_phantom_identity(tmp_path: Path) -> None:
    lib = Library(*_make_archive(tmp_path))
    ghost = lib.lookup(701203, 2).replay("http://127.0.0.1:54908")["ghostRuns"][0]
    assert ghost["userName"] == "TH3 P1ON33R"
    assert ghost["lifetime"] == 235.164


def test_unknown_temple_is_none(tmp_path: Path) -> None:
    lib = Library(*_make_archive(tmp_path))
    assert lib.lookup(999999, 0) is None


def test_asset_path_rejects_traversal(tmp_path: Path) -> None:
    assets, fixtures = _make_archive(tmp_path)
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    lib = Library(assets, fixtures)
    assert lib.asset_path("../secret.txt") is None
    assert lib.asset_path(LAYOUT) is not None


def test_missing_archive_directory_is_safe(tmp_path: Path) -> None:
    lib = Library(tmp_path / "nope", tmp_path / "also-nope")
    assert lib.lookup(1, 0) is None
    assert "0 temple" in lib.summary()


def test_every_phantom_on_a_floor_spawns_under_its_own_name(tmp_path: Path) -> None:
    """The client names the actor after the runner, so ids must be unique.

    Two phantoms sharing an id are two actors sharing a name, and the client
    does not survive it:

        LowLevelFatalError [Line: 417] An actor of name 'Ghost0' already
        exists in level 'Main_Temple'

    Reachable two ways. Pooled recordings carry no account fields at all, so
    every one resolved to Ghost0; and a player who ran the same floor twice
    leaves two recordings under their own id, which crashed real archives.
    """
    temple = Temple(dungeon_id=701701, floor=0)
    temple.ghosts = ["pool__a", "pool__b", "mine-1", "mine-2"]
    temple.ghost_meta = {
        "pool__a": {"success": 1},                 # pooled: no id at all
        "pool__b": {"success": 0},
        "mine-1": {"userID": 62842, "success": 1},  # the same player, twice
        "mine-2": {"userID": 62842, "success": 0},
    }

    runs = temple.ghost_runs("http://127.0.0.1:1")
    names = [f"Ghost{r['userID']}" for r in runs]
    assert len(set(names)) == len(names), names
    assert all(r.get("userID") for r in runs), "no phantom may spawn as Ghost0"
    assert runs[2]["userID"] == 62842, "the first real id is kept as it is"
    assert runs[3]["userID"] != 62842, "the duplicate is given one of its own"


def test_a_stand_in_id_is_stable(tmp_path: Path) -> None:
    """A client that remembers a phantom must still recognize it next time."""
    temple = Temple(dungeon_id=701701, floor=0)
    temple.ghosts = ["pool__a"]
    temple.ghost_meta = {"pool__a": {}}
    first = temple.ghost_runs("http://127.0.0.1:1")[0]
    second = temple.ghost_runs("http://127.0.0.1:2")[0]
    assert first["userID"] == second["userID"]
    assert first["runID"] != first["userID"]


def test_ghost_metadata_is_looked_up_not_searched(tmp_path: Path) -> None:
    """Finding an archived file must not walk the whole archive each time.

    It did: for every phantom in every captured response, `_stored_name` built
    a list of every filename in the archive and scanned it. On a real archive
    that is roughly 23,000 lookups across 25,000 names -- some 575 million
    comparisons, thirteen seconds, on the thread the window waits for.

    Correctness is the point of the test; the timing is a guard against the
    quadratic shape coming back, so the bound is deliberately loose.
    """
    import time

    assets = tmp_path / "assets"
    fixtures = tmp_path / "fixtures"
    assets.mkdir(parents=True)
    fixtures.mkdir(parents=True)

    ghosts = 400
    rows = []
    for dungeon in range(20):
        layout = f"version_128__2026-04-27__dungeon-{dungeon}-floor-0-layout-aa"
        (assets / layout).write_text("layout", encoding="utf-8")
        runs = []
        for i in range(ghosts // 20):
            tail = f"dungeon-{dungeon}-floor-0-runinfo-{i:04d}"
            (assets / f"version_128__2026-04-27__{tail}").write_text("g", "utf-8")
            runs.append({
                "downloadURL": f"https://dungeons.wiby.net/version_128/2026-04-27/{tail}",
                "userName": f"runner{i}", "userID": 1000 + i, "success": 1,
            })
        rows.append(json.dumps({
            "status": 200,
            "response_body": {"dungeonID": dungeon, "dungeonFloorNumber": 0,
                              "ghostRuns": runs},
        }))
    (fixtures / "GetDungeon.jsonl").write_text("\n".join(rows), encoding="utf-8")

    started = time.perf_counter()
    library = Library(assets, fixtures)
    elapsed = time.perf_counter() - started

    named = sum(
        1
        for temple in library.temples.values()
        for meta in temple.ghost_meta.values()
        if meta.get("userName")
    )
    assert named == ghosts, "every phantom must still find its metadata"
    assert elapsed < 5.0, f"indexing went quadratic again ({elapsed:.1f}s)"
