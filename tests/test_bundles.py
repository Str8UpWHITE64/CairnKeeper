"""Shared content is the player's choice, and never arrives uninvited.

A player's own captures are theirs. A bundle is somebody else's work offered to
them, and both answers are reasonable: some want their offline copy to be
exactly what they played, others want every temple anyone managed to save.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import bundles  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

LAYOUT = "eyJudW1XaW5ncyI6MX0="


def _make_bundle(state: Path, key: str, dungeons=(900001,), *, ghosts=2) -> Path:
    path = state / bundles.BUNDLES_DIR / key
    path.mkdir(parents=True, exist_ok=True)
    for dungeon in dungeons:
        (path / f"version_128__2026-08-15__dungeon-{dungeon}-floor-0-layout-x"
         ).write_text(LAYOUT, encoding="utf-8")
        for g in range(ghosts):
            (path / f"version_128__2026-08-15__dungeon-{dungeon}-floor-0-runinfo-{g}"
             ).write_text("phantom", encoding="utf-8")
    (path / "bundle.json").write_text(
        json.dumps({"name": key, "description": "temples other people saved"}),
        encoding="utf-8",
    )
    return path


def test_a_new_bundle_starts_switched_off(tmp_path: Path) -> None:
    """Content arriving without being chosen is what this prevents."""
    _make_bundle(tmp_path, "community-2026-08")
    found = bundles.discover(tmp_path)
    assert len(found) == 1
    assert found[0].enabled is False
    assert bundles.enabled_paths(tmp_path) == []


def test_switching_one_on_sticks(tmp_path: Path) -> None:
    _make_bundle(tmp_path, "community-2026-08")
    assert bundles.set_enabled(tmp_path, "community-2026-08", True)
    assert [p.name for p in bundles.enabled_paths(tmp_path)] == ["community-2026-08"]
    assert bundles.discover(tmp_path)[0].enabled is True


def test_switching_one_off_again_sticks(tmp_path: Path) -> None:
    _make_bundle(tmp_path, "community-2026-08")
    bundles.set_enabled(tmp_path, "community-2026-08", True)
    bundles.set_enabled(tmp_path, "community-2026-08", False)
    assert bundles.enabled_paths(tmp_path) == []


def test_an_unknown_bundle_is_refused(tmp_path: Path) -> None:
    assert bundles.set_enabled(tmp_path, "not-installed", True) is False


def test_an_enabled_bundle_adds_temples(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    _make_bundle(tmp_path, "community", dungeons=(900001, 900002))
    bundles.set_enabled(tmp_path, "community", True)

    own = Library(assets, tmp_path / "fx")
    assert len(own.temples) == 0

    with_shared = Library(assets, tmp_path / "fx",
                          bundles=bundles.enabled_paths(tmp_path))
    assert len(with_shared.temples) == 2
    assert sum(len(t.ghosts) for t in with_shared.temples.values()) == 4


def test_the_players_own_copy_wins(tmp_path: Path) -> None:
    """Their copy carries their phantoms and their history with it."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "version_128__2026-08-20__dungeon-900001-floor-0-layout-mine"
     ).write_text(LAYOUT, encoding="utf-8")
    _make_bundle(tmp_path, "community", dungeons=(900001,))
    bundles.set_enabled(tmp_path, "community", True)

    lib = Library(assets, tmp_path / "fx", bundles=bundles.enabled_paths(tmp_path))
    assert lib.temples[(900001, 0)].layout.endswith("layout-mine")


def test_a_bundle_cannot_name_a_file_outside_itself(tmp_path: Path) -> None:
    """A bundle is content from strangers; it does not get to pick paths."""
    assets = tmp_path / "assets"
    assets.mkdir()
    secret = tmp_path / "credentials.json"
    secret.write_text("a live auth ticket", encoding="utf-8")
    _make_bundle(tmp_path, "community")
    bundles.set_enabled(tmp_path, "community", True)

    lib = Library(assets, tmp_path / "fx", bundles=bundles.enabled_paths(tmp_path))
    for attempt in ("../credentials.json", "../../credentials.json",
                    r"..\credentials.json"):
        assert lib.asset_path(attempt) is None, f"escaped with {attempt!r}"


def test_a_bundle_blob_still_resolves(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    _make_bundle(tmp_path, "community")
    bundles.set_enabled(tmp_path, "community", True)
    lib = Library(assets, tmp_path / "fx", bundles=bundles.enabled_paths(tmp_path))
    name = lib.temples[(900001, 0)].layout
    resolved = lib.asset_path(name)
    assert resolved is not None and resolved.read_text("utf-8") == LAYOUT


def test_the_summary_says_shared_content_is_in_use(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    _make_bundle(tmp_path, "community")
    bundles.set_enabled(tmp_path, "community", True)
    lib = Library(assets, tmp_path / "fx", bundles=bundles.enabled_paths(tmp_path))
    assert "shared bundle" in lib.summary()
