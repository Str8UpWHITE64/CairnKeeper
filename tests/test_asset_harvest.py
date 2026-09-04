"""Live capture must notice blob URLs as responses go by.

Archiving while playing is the only way to preserve a temple -- /GetDungeon
will not re-serve a finished route -- so a silent failure here loses content
permanently.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.assets import AssetArchiver, iter_urls, local_name  # noqa: E402

REAL_RESPONSE = {
    "dungeonID": 701203,
    "dungeonSeed": -951959203,
    "layoutDownloadURL": "https://dungeons.wiby.net/version_128/2024-02-23/"
    "dungeon-701203-floor-2-layout-ygck77l0",
    "ghostRuns": [
        {
            "userName": "TH3 P1ON33R",
            "downloadURL": "https://dungeons.wiby.net/version_128/2026-04-27/"
            "dungeon-701203-floor-2-runinfo-lttxkd70",
        },
        {
            "userName": "Dan",
            "downloadURL": "https://dungeons.wiby.net/version_128/2026-04-30/"
            "dungeon-701203-floor-2-runinfo-cmiuchdn",
        },
    ],
    "precalculatedRooms": None,
}


def test_finds_layout_and_every_phantom() -> None:
    urls = list(iter_urls(REAL_RESPONSE))
    assert len(urls) == 3
    assert any("layout" in u for u in urls)
    assert sum("runinfo" in u for u in urls) == 2


def test_ignores_non_url_fields() -> None:
    urls = list(iter_urls(REAL_RESPONSE))
    assert not any(u in ("", "None") for u in urls)
    assert all(u.startswith("https://") for u in urls)


def test_empty_urls_skipped() -> None:
    assert list(iter_urls({"layoutDownloadURL": "", "downloadURL": None})) == []


def test_local_name_preserves_identity() -> None:
    name = local_name(REAL_RESPONSE["layoutDownloadURL"])
    assert "dungeon-701203-floor-2-layout-ygck77l0" in name
    assert "/" not in name


def test_harvest_queues_each_url_once(tmp_path: Path) -> None:
    archiver = AssetArchiver(tmp_path, workers=0)
    assert archiver.harvest(REAL_RESPONSE) == 3
    # Re-harvesting the same response must not queue duplicates.
    assert archiver.harvest(REAL_RESPONSE) == 0


def test_harvest_survives_unexpected_shapes(tmp_path: Path) -> None:
    archiver = AssetArchiver(tmp_path, workers=0)
    for payload in (None, [], "text", 42, {"nested": [{"downloadURL": ""}]}):
        assert archiver.harvest(payload) == 0


def test_index_round_trips(tmp_path: Path) -> None:
    archiver = AssetArchiver(tmp_path, workers=0)
    archiver.harvest(REAL_RESPONSE)
    # Index file is created lazily on first fetch; absence must not crash.
    reopened = AssetArchiver(tmp_path, workers=0)
    assert isinstance(reopened.harvest(REAL_RESPONSE), int)


def test_nested_ghost_runs_reached() -> None:
    """Phantom URLs live inside a list of dicts, not at the top level."""
    urls = json.dumps(list(iter_urls({"ghostRuns": REAL_RESPONSE["ghostRuns"]})))
    assert "lttxkd70" in urls and "cmiuchdn" in urls


def _asset_threads() -> int:
    import threading

    return sum(1 for t in threading.enumerate() if t.name.startswith("asset-"))


def test_nothing_to_download_starts_no_workers(tmp_path: Path, monkeypatch) -> None:
    """Most things that build an archiver never download anything.

    A share lookup that comes back empty, a session where the game asks for
    nothing new. Four idle threads apiece left twenty running at the end of a
    test run, which is the shape of thing that brings an interpreter down on
    the way out.
    """
    # The workers would otherwise fetch the URLs in REAL_RESPONSE, which are
    # the real ones, off the real host.
    monkeypatch.setattr(AssetArchiver, "_fetch", lambda self, url: None)

    before = _asset_threads()
    archiver = AssetArchiver(tmp_path / "quiet")
    assert _asset_threads() == before, "built one and started nothing"

    archiver.harvest(REAL_RESPONSE)
    assert _asset_threads() == before + 4, "and started them when asked to work"

    archiver.stop()
    assert _asset_threads() == before, "and stopped them"
    archiver.stop()
    assert _asset_threads() == before, "twice is fine -- two servers share one"
