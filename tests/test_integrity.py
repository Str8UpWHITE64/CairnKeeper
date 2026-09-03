"""A truncated file looks archived right up until you need it.

By then the service it came from is gone, so the check has to happen while
there is still something to re-download from -- which means it has to be cheap
enough to run without anybody deciding to. A full sweep of a real 6.4 GB
archive took 3m53s; a session adds a few dozen files.
"""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import integrity  # noqa: E402

LAYOUT = base64.b64encode(
    json.dumps({"numWings": 2, "roomInfos": [{"roomIndex": 0}]}).encode()
).decode()


def _archive(tmp_path: Path, blobs: int = 3) -> Path:
    import hashlib

    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    index = {}
    for i in range(blobs):
        name = f"version_128__2026-08-15__dungeon-70000{i}-floor-0-layout-aa"
        (assets / name).write_text(LAYOUT, encoding="utf-8")
        raw = (assets / name).read_bytes()
        index[f"https://dungeons.wiby.net/{name}"] = {
            "file": name, "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    (assets / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return tmp_path


def test_a_healthy_archive_checks_out(tmp_path: Path) -> None:
    result = integrity.check(_archive(tmp_path))
    assert result.ok
    assert result.checked == 3
    assert result.trusted == 0
    assert "intact" in result.summary()


def test_the_second_check_reads_almost_nothing(tmp_path: Path) -> None:
    """The whole reason this can run after every session."""
    root = _archive(tmp_path, blobs=5)
    integrity.check(root)
    again = integrity.check(root)
    assert again.checked == 0
    assert again.trusted == 5
    assert again.ok
    assert "unchanged since last time" in again.summary()


def test_a_file_added_since_the_last_check_is_read(tmp_path: Path) -> None:
    import hashlib

    root = _archive(tmp_path, blobs=2)
    integrity.check(root)

    assets = root / "assets"
    name = "version_128__2026-08-15__dungeon-999999-floor-0-layout-zz"
    (assets / name).write_text(LAYOUT, encoding="utf-8")
    index = json.loads((assets / "index.json").read_text("utf-8"))
    raw = (assets / name).read_bytes()
    index[f"https://dungeons.wiby.net/{name}"] = {
        "file": name, "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest()}
    (assets / "index.json").write_text(json.dumps(index), encoding="utf-8")

    result = integrity.check(root)
    assert result.checked == 1, "only the new one"
    assert result.trusted == 2


def test_damage_after_a_clean_check_is_still_caught(tmp_path: Path) -> None:
    """The ledger must never be able to vouch for a file that has changed."""
    root = _archive(tmp_path, blobs=3)
    assert integrity.check(root).ok

    hurt = next(p for p in (root / "assets").iterdir() if p.name != "index.json")
    time.sleep(0.01)
    hurt.write_text("truncated", encoding="utf-8")

    result = integrity.check(root)
    assert not result.ok
    assert any("mismatch" in p for p in result.problems)


def test_a_full_sweep_ignores_the_ledger(tmp_path: Path) -> None:
    root = _archive(tmp_path, blobs=4)
    integrity.check(root)
    full = integrity.check(root, everything=True)
    assert full.checked == 4
    assert full.trusted == 0


def test_a_layout_that_will_not_decode_is_a_problem(tmp_path: Path) -> None:
    """Exactly the fault found in a real archive: a layout with no rooms."""
    import hashlib

    root = _archive(tmp_path, blobs=1)
    assets = root / "assets"
    name = "version_128__2026-08-15__dungeon-248925-floor-0-layout-bad"
    empty = base64.b64encode(json.dumps({"numWings": 1}).encode()).decode()
    (assets / name).write_text(empty, encoding="utf-8")
    index = json.loads((assets / "index.json").read_text("utf-8"))
    raw = (assets / name).read_bytes()
    index[f"https://dungeons.wiby.net/{name}"] = {
        "file": name, "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest()}
    (assets / "index.json").write_text(json.dumps(index), encoding="utf-8")

    result = integrity.check(root)
    assert any("no roomInfos" in p for p in result.problems)


def test_an_empty_archive_is_not_a_failure(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir(parents=True)
    result = integrity.check(tmp_path)
    assert result.ok
    assert "Nothing archived yet" in result.summary()


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    root = _archive(tmp_path, blobs=2)
    gone = next(p for p in (root / "assets").iterdir() if p.name != "index.json")
    gone.unlink()
    result = integrity.check(root)
    assert any("missing file" in p for p in result.problems)
