"""The half of a player's progress that lives on their own disk.

Progression is in two places, which is not obvious and cost a round of testing
to find. The server holds reputation, victory routes and purchases. The client
holds the rest in `UserProgress.sav` -- whips, upgrades, relics, essence, keys --
and reads it at startup without asking anybody.

So a profile that only swaps the server-side save resets almost nothing the
player can see. To make a second profile a real second profile, the local save
has to travel with it: put the profile's save in place before the game starts,
take it back afterwards, and leave the file as it was found.

Everything here is a copy, never a move, and the file that was there before a
session is kept until it has been put back. Losing this file loses whips and
relics that took weeks to earn, and there is no server-side copy to recover it
from -- so the safe order is: keep the old one, then write the new one.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SAVE_NAME = "UserProgress.sav"
STORE = "saves"
BOOKMARK = "state.json"
# What was live when a session started, kept until it is back where it was.
IN_FLIGHT = "_before_session.sav"


def game_save_dir() -> Path | None:
    """Where the game keeps its progression, if it is there to be found."""
    import os

    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    folder = Path(local) / "PhantomAbyss" / "Saved" / "SaveGames"
    return folder if folder.is_dir() else None


class SaveSlots:
    """One stored local save per profile, and the swap around a session."""

    #: Passed as `save_dir` to mean "find the game's own folder". Absence has
    #: to be spelled, because it used to be spelled `None` -- and `None` also
    #: meant "look it up", so a test that meant to work on nothing at all
    #: deleted a real save instead. There is no server-side copy of that file.
    FIND = object()

    def __init__(self, state_dir: Path, save_dir: Path | None | Any = FIND):
        self.dir = Path(state_dir) / STORE
        self.dir.mkdir(parents=True, exist_ok=True)
        self.save_dir = game_save_dir() if save_dir is self.FIND else save_dir
        self._bookmark = self.dir / BOOKMARK

    # ------------------------------------------------------------- state

    @property
    def live(self) -> Path | None:
        return (self.save_dir / SAVE_NAME) if self.save_dir else None

    def _read_bookmark(self) -> dict[str, Any]:
        try:
            data = json.loads(self._bookmark.read_text("utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _write_bookmark(self, data: dict[str, Any]) -> None:
        self._bookmark.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def owner(self) -> str:
        """Which profile the save currently sitting in the game belongs to."""
        return str(self._read_bookmark().get("owner") or "")

    def stored(self, handle: str) -> Path:
        return self.dir / f"{handle}.sav"

    def has(self, handle: str) -> bool:
        return self.stored(handle).is_file()

    # -------------------------------------------------------------- swap

    def install(self, handle: str, presumed_owner: str = "") -> str:
        """Put this profile's save in place for a session.

        `presumed_owner` says whose the save already on disk is, for the first
        session after profiles arrive -- nothing recorded who it belonged to,
        because until then nobody needed to ask. Without it an unclaimed save
        belongs to no one, and the player's own main profile is handed a fresh
        start on top of their real progress.

        Returns a line worth showing the player, or "" when nothing was done.
        """
        live = self.live
        if not handle or live is None:
            return ""

        current = self.owner() or presumed_owner
        if current == handle and live.exists() and not self.has(handle):
            # The save on disk is this profile's, and nothing has been stored
            # for them yet. Keep it exactly where it is; it is their progress.
            self._begin(live, current)
            book = self._read_bookmark()
            book["owner"] = handle
            self._write_bookmark(book)
            return ""
        if current == handle and not self._read_bookmark().get("inFlight"):
            # Already theirs. Still keep a copy of what is live, so the
            # session can be undone whatever happens during it.
            self._begin(live, current)
            return ""

        self._begin(live, current)

        target = self.stored(handle)
        if target.is_file():
            shutil.copy2(target, live)
            note = "your saved progress for this profile is in place"
        elif live.exists():
            # No stored save: this profile has never been played, so the live
            # file has to go for the game to build a fresh one. It is only ever
            # removed once a copy has been written AND read back -- there is no
            # other copy of a player's whips and relics anywhere.
            keep = self.dir / IN_FLIGHT
            if not self._is_copy_of(keep, live):
                raise RuntimeError(
                    "refusing to clear the live save: no verified copy was kept"
                )
            live.unlink()
            note = "starting this profile from the beginning"
        else:
            note = ""

        book = self._read_bookmark()
        book["owner"] = handle
        self._write_bookmark(book)
        return note

    @staticmethod
    def _is_copy_of(copy: Path, original: Path) -> bool:
        """Byte-for-byte, read back from disk rather than assumed."""
        try:
            if not copy.is_file() or not original.is_file():
                return False
            if copy.stat().st_size != original.stat().st_size:
                return False
            return (hashlib.sha256(copy.read_bytes()).digest()
                    == hashlib.sha256(original.read_bytes()).digest())
        except OSError:
            return False

    def _begin(self, live: Path, current: str) -> None:
        """Record what was live before the session, once."""
        book = self._read_bookmark()
        if book.get("inFlight"):
            return
        keep = self.dir / IN_FLIGHT
        if live.exists():
            shutil.copy2(live, keep)
            # And bank it under whoever it belonged to, so switching away
            # never loses the progress made under the previous profile.
            if current:
                shutil.copy2(live, self.stored(current))
        elif keep.exists():
            keep.unlink()
        book["inFlight"] = True
        book["restoreTo"] = current
        book["startedAt"] = datetime.now(timezone.utc).replace(
            microsecond=0).isoformat()
        self._write_bookmark(book)

    def finish(self) -> str:
        """Bank what the session produced, then put the file back as found."""
        live = self.live
        book = self._read_bookmark()
        if live is None or not book.get("inFlight"):
            return ""

        handle = str(book.get("owner") or "")
        banked = ""
        if handle and live.exists():
            shutil.copy2(live, self.stored(handle))
            banked = "progress for this profile is saved"

        # Put back the save belonging to whoever held it before this session --
        # from their stored copy, not from the snapshot taken at the start.
        #
        # Restoring the snapshot looked equivalent and was not: after a session
        # played on that same profile, the snapshot is one session out of date,
        # and the next switch banks it back over the newer copy. A relic earned
        # on the main profile disappeared the next time the player changed
        # profile, which is the worst kind of bug -- silent, and only visible
        # much later.
        restore_to = str(book.get("restoreTo") or "")
        keep = self.dir / IN_FLIGHT
        source = self.stored(restore_to) if restore_to else None
        if source is not None and source.is_file():
            shutil.copy2(source, live)
        elif keep.exists():
            shutil.copy2(keep, live)
        elif live.exists():
            live.unlink()
        keep.unlink(missing_ok=True)

        self._write_bookmark({"owner": restore_to})
        return banked

    def recover(self) -> str:
        """Put a save back after a session that never finished.

        The game crashing, or the power going out, leaves the profile's save
        sitting where the player's own belongs. Restoring at startup means it
        fixes itself rather than needing anybody to know it happened.
        """
        book = self._read_bookmark()
        if not book.get("inFlight"):
            return ""
        return self.finish() and "put your save back after an interrupted session"
