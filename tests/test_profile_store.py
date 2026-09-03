"""One profile per player.

A self-hosted server serves more than one person, and a second account must not
inherit the first player's progression just because a capture existed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.profile import ProfileStore  # noqa: E402

VETERAN = "76561198000000042"
NEWCOMER = "76561198000000044"

CAPTURED = {
    "currency": {"essence": 1167, "dungeonKeys": [281, 13, 11, 1]},
    "reputation": 5582,
    "permanentPurchases": ["a", "b", "c"],
    "currentUsername": "Tomb Raider",
}


def test_seed_owner_inherits_capture(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, seed=CAPTURED, seed_owner=VETERAN)
    assert store.for_player(VETERAN).snapshot()["reputation"] == 5582


def test_other_player_starts_fresh(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, seed=CAPTURED, seed_owner=VETERAN)
    fresh = store.for_player(NEWCOMER).snapshot()
    assert fresh["reputation"] == 0
    assert fresh["currency"]["essence"] == 0
    assert fresh["permanentPurchases"] == []


def test_players_do_not_share_progression(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, seed=CAPTURED, seed_owner=VETERAN)
    store.for_player(NEWCOMER).apply_run(
        {"success": 2, "currency": {"essence": 0, "dungeonKeys": [7, 0, 0, 0]}}
    )
    assert store.for_player(VETERAN).snapshot()["currency"]["dungeonKeys"] == [
        281, 13, 11, 1
    ]
    assert store.for_player(NEWCOMER).snapshot()["currency"]["dungeonKeys"] == [
        7, 0, 0, 0
    ]


def test_same_player_gets_the_same_profile(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    assert store.for_player(VETERAN) is store.for_player(VETERAN)


def test_profiles_persist_separately(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path, seed=CAPTURED, seed_owner=VETERAN)
    store.for_player(NEWCOMER).apply_run({"success": 2, "reputation": 7})
    reopened = ProfileStore(tmp_path, seed=CAPTURED, seed_owner=VETERAN)
    assert reopened.for_player(NEWCOMER).snapshot()["reputation"] == 7
    assert reopened.for_player(VETERAN).snapshot()["reputation"] == 5582
    assert set(reopened.known_players()) == {VETERAN, NEWCOMER}


def test_missing_player_id_is_handled(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    assert store.for_player(None) is store.for_player("")


def test_player_id_cannot_escape_the_directory(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    store.for_player("../../evil").apply_run({"success": 2, "reputation": 1})
    assert not (tmp_path.parent / "evil.json").exists()
    assert store.known_players(), "should have written somewhere inside the store"


def test_no_seed_means_everyone_is_fresh(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    assert store.for_player(VETERAN).snapshot()["reputation"] == 0
