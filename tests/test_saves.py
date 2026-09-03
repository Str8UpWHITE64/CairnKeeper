"""A profile has to carry the save the game keeps on disk.

Progression lives in two places. The server holds reputation, victory routes
and purchases; `UserProgress.sav` holds whips, upgrades, relics and currency,
and the client reads it at startup without asking anybody. Profiles were built
against the server half only, so a second profile showed the first one's whips
-- the server said the save was empty and the game showed everything anyway.

There is no server-side copy of this file. Every test here is really asking the
same question: can the player get their own save back?
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.saves import SAVE_NAME, SaveSlots  # noqa: E402

MAIN = "pa-main"
SECOND = "pa-second"


def _game(tmp_path: Path, contents: str = "main progress") -> Path:
    game = tmp_path / "SaveGames"
    game.mkdir(parents=True)
    (game / SAVE_NAME).write_text(contents, encoding="utf-8")
    return game


def _slots(tmp_path: Path, game: Path) -> SaveSlots:
    return SaveSlots(tmp_path / "state", save_dir=game)


def test_a_new_profile_starts_without_the_old_ones_save(tmp_path: Path) -> None:
    game = _game(tmp_path)
    slots = _slots(tmp_path, game)
    slots.install(MAIN)
    slots.finish()

    slots.install(SECOND)
    assert not (game / SAVE_NAME).exists(), "the game must build a fresh one"


def test_the_save_that_was_there_is_put_back(tmp_path: Path) -> None:
    """The file is left as it was found, like the patched executable is."""
    game = _game(tmp_path, "main progress")
    slots = _slots(tmp_path, game)

    slots.install(SECOND)
    (game / SAVE_NAME).write_text("second profile progress", encoding="utf-8")
    slots.finish()

    assert (game / SAVE_NAME).read_text("utf-8") == "main progress"


def test_progress_made_on_a_profile_comes_back_to_it(tmp_path: Path) -> None:
    game = _game(tmp_path, "main progress")
    slots = _slots(tmp_path, game)

    slots.install(SECOND)
    (game / SAVE_NAME).write_text("second profile progress", encoding="utf-8")
    slots.finish()

    slots.install(SECOND)
    assert (game / SAVE_NAME).read_text("utf-8") == "second profile progress"
    slots.finish()
    assert (game / SAVE_NAME).read_text("utf-8") == "main progress"


def test_switching_away_banks_what_the_last_profile_earned(tmp_path: Path) -> None:
    """Whatever was live belonged to somebody, and must not be dropped."""
    game = _game(tmp_path, "main progress")
    slots = _slots(tmp_path, game)

    slots.install(MAIN)
    (game / SAVE_NAME).write_text("main, now with a new whip", encoding="utf-8")
    slots.finish()

    slots.install(SECOND)
    slots.finish()
    slots.install(MAIN)
    assert (game / SAVE_NAME).read_text("utf-8") == "main, now with a new whip"


def test_an_interrupted_session_is_repaired_at_startup(tmp_path: Path) -> None:
    """A crash leaves the wrong save where the player's own belongs."""
    game = _game(tmp_path, "main progress")
    slots = _slots(tmp_path, game)

    slots.install(SECOND)
    (game / SAVE_NAME).write_text("second profile progress", encoding="utf-8")
    # No finish(): the game crashed, or the machine went off.

    repaired = SaveSlots(tmp_path / "state", save_dir=game)
    assert repaired.recover()
    assert (game / SAVE_NAME).read_text("utf-8") == "main progress"
    assert repaired.stored(SECOND).read_text("utf-8") == "second profile progress"


def test_recovery_does_nothing_when_nothing_is_in_flight(tmp_path: Path) -> None:
    game = _game(tmp_path)
    slots = _slots(tmp_path, game)
    assert slots.recover() == ""
    assert (game / SAVE_NAME).read_text("utf-8") == "main progress"


def test_the_original_is_kept_before_anything_is_written(tmp_path: Path) -> None:
    """The order that matters: keep the old one, then write the new one."""
    game = _game(tmp_path, "irreplaceable")
    slots = _slots(tmp_path, game)
    slots.install(SECOND)

    kept = list((tmp_path / "state" / "saves").glob("*.sav"))
    assert any(p.read_text("utf-8") == "irreplaceable" for p in kept), \
        "the save that was there must exist somewhere before it is replaced"


def test_no_save_directory_is_not_an_error(tmp_path: Path) -> None:
    """Somebody may not have run the game yet."""
    slots = SaveSlots(tmp_path / "state", save_dir=None)
    assert slots.install(MAIN) == ""
    assert slots.finish() == ""


def test_no_directory_never_means_the_real_one(tmp_path: Path) -> None:
    """`None` used to fall through to the player's own save folder.

    This test, meaning to operate on nothing, deleted a live UserProgress.sav
    on the machine it ran on -- whips, upgrades and relics, with no server-side
    copy to recover from. Absence must be absence.
    """
    slots = SaveSlots(tmp_path / "state", save_dir=None)
    assert slots.save_dir is None
    assert slots.live is None


def test_a_profile_with_no_stored_save_is_left_alone_on_finish(
    tmp_path: Path,
) -> None:
    game = tmp_path / "SaveGames"
    game.mkdir(parents=True)
    slots = _slots(tmp_path, game)
    slots.install(SECOND)
    slots.finish()
    assert not (game / SAVE_NAME).exists()


def test_the_bookmark_records_who_the_live_save_belongs_to(tmp_path: Path) -> None:
    game = _game(tmp_path)
    slots = _slots(tmp_path, game)
    slots.install(SECOND)
    assert slots.owner() == SECOND
    slots.finish()
    book = json.loads((tmp_path / "state" / "saves" / "state.json").read_text("utf-8"))
    assert not book.get("inFlight")


def test_the_live_save_is_never_cleared_without_a_verified_copy(
    tmp_path: Path, monkeypatch
) -> None:
    """The rule that would have prevented deleting a real player's save.

    Clearing the live file is how a fresh profile starts, so the deletion is
    intended -- but it must be impossible unless a copy has been written and
    read back byte for byte.
    """
    import pytest

    game = _game(tmp_path, "irreplaceable")
    slots = _slots(tmp_path, game)

    # A copy that silently does not happen.
    monkeypatch.setattr("phantom_offline.saves.shutil.copy2",
                        lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="verified copy"):
        slots.install(SECOND)
    assert (game / SAVE_NAME).read_text("utf-8") == "irreplaceable"


def test_progress_earned_on_a_profile_is_not_lost_at_the_next_switch(
    tmp_path: Path,
) -> None:
    """The subtle one, and the reason restoring a snapshot is wrong.

    Playing a profile leaves newer progress live. Restoring the snapshot taken
    at the start of that session put a stale file back, and the next switch
    banked the stale file over the newer stored copy -- so a relic earned on
    the main profile vanished the next time the player changed profile.
    """
    game = _game(tmp_path, "main")
    slots = _slots(tmp_path, game)

    slots.install(MAIN, presumed_owner=MAIN)
    (game / SAVE_NAME).write_text("main + a new relic", encoding="utf-8")
    slots.finish()

    slots.install(SECOND, presumed_owner=MAIN)
    slots.finish()

    slots.install(MAIN, presumed_owner=MAIN)
    assert (game / SAVE_NAME).read_text("utf-8") == "main + a new relic"


def test_the_save_on_disk_is_recognised_as_the_first_profiles(
    tmp_path: Path,
) -> None:
    """Before profiles existed nothing recorded whose the save was.

    Treating it as nobody's handed the player a fresh start on top of their
    own progress the first time they played their main profile.
    """
    game = _game(tmp_path, "everything earned so far")
    slots = _slots(tmp_path, game)
    slots.install(MAIN, presumed_owner=MAIN)
    assert (game / SAVE_NAME).read_text("utf-8") == "everything earned so far"
    slots.finish()
    assert (game / SAVE_NAME).read_text("utf-8") == "everything earned so far"
