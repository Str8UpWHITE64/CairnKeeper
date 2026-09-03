"""Who the player is, as far as any server is concerned.

The game signs in with a SteamID64 and a live Steam authentication ticket. That
is correct when talking to Steam's own service. It is wrong when talking to a
preservation server run by a stranger, which needs neither: it needs something
stable to file a save under, and nothing more.

So the client substitutes. Before a request leaves for a server it swaps the
platform id for a handle minted on this machine, and swaps it back in the reply
so the game sees the account it expects. The server stores progress under the
handle and never learns a platform id at all.

Why a minted handle and not a hash of the Steam id
--------------------------------------------------
Because a hash of an enumerable input is not anonymous. A SteamID64 is 7656119
followed by ten digits, and the range in actual use is far narrower; anybody
holding the hashes can hash every candidate and recover the list. That is
obfuscation dressed as privacy. A handle drawn from the system random source
has no preimage to find.

The cost, stated plainly: the handle is the save. Lose identity.json and the
server has no way to know you are the same person, because that is precisely
what it was built not to know. It is small, it is readable, and it belongs in
whatever the player backs up.
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FILE = "identity.json"
PREFIX = "pa"
DEFAULT_SLOT = "Main"

# Fields carrying the player's own platform id, swapped for the handle.
OWN_IDENTITY = ("playerid", "platformid")

# Live credentials. A preservation server cannot check a Steam ticket and has
# no reason to hold one, so it never receives it.
CREDENTIALS = ("verificationtoken", "platformverificationid")

# Other people's platform ids: friend lists, people to add. Sending these would
# hand a server the player's Steam friends list, which is not the player's to
# give and not the server's to have.
THIRD_PARTIES = (
    "platformfriends", "friendplatformids", "userstoadd", "usertoadd",
    "ghostids", "sharerid", "shareruserid",
)


def mint() -> str:
    """A handle that means nothing anywhere else."""
    return f"{PREFIX}-{secrets.token_hex(16)}"


class Identity:
    """The handle this machine presents, and the swap in both directions."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / FILE
        self._lock = threading.Lock()
        self._stamp = self._mtime()
        self.data = self._load()

    def _mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def _refresh(self) -> None:
        """Pick up a profile chosen elsewhere since this was loaded.

        The window writes the choice; a server already running holds its own
        copy in memory. Without this, switching profiles kept serving the old
        one until the whole program was restarted -- which looks exactly like
        the switch being ignored, and is indistinguishable from a bug in the
        switching itself.
        """
        stamp = self._mtime()
        if stamp != self._stamp:
            self._stamp = stamp
            self.data = self._load()

    def handle_for(self, real: str) -> str:
        """The handle this account is currently playing under.

        One per account rather than one per machine. Two people sharing a PC --
        or one person with a second account, which is how this was tested --
        are two players, and merging their saves because they share a hard
        drive would be a strange way to preserve anybody's progress.

        An account can have several profiles; this is whichever is selected.
        The map lives here and is never sent: a server receives a handle and
        has nothing to map it back with.
        """
        key = str(real or "").strip()
        if not key:
            return ""
        with self._lock:
            self._refresh()
            record = self._record(key)
            return str(record["active"])

    # ------------------------------------------------------------- profiles

    def _record(self, key: str) -> dict[str, Any]:
        """This account's profiles, created or upgraded on demand.

        Call with the lock held. An earlier build stored one handle per
        account; that handle becomes the account's first profile so nobody's
        save moves when profiles arrive.
        """
        players = self.data.setdefault("players", {})
        record = players.get(key)
        if isinstance(record, str) and record:
            record = {"active": record,
                      "slots": [{"name": DEFAULT_SLOT, "handle": record}]}
            players[key] = record
            self._write(self.data)
        if not isinstance(record, dict) or not record.get("slots"):
            handle = mint()
            record = {"active": handle,
                      "slots": [{"name": DEFAULT_SLOT, "handle": handle}]}
            players[key] = record
            self._write(self.data)
        return record

    def profiles_for(self, real: str) -> list[dict[str, Any]]:
        """Every profile this account has, newest last, active flagged."""
        key = str(real or "").strip()
        if not key:
            return []
        with self._lock:
            record = self._record(key)
            return [
                {**slot, "active": slot.get("handle") == record.get("active")}
                for slot in record["slots"]
            ]

    def accounts(self) -> list[str]:
        """The accounts this machine has seen, so the window can offer them."""
        return sorted(str(k) for k in (self.data.get("players") or {}))

    def create_profile(self, real: str, name: str) -> str:
        """Start a fresh profile. Nothing existing is touched.

        This is the thing players have asked for since release: begin again
        without buying the game a second time, and without losing the save
        they already have. A new handle is a save the server has never seen,
        so it answers with what it sends a first-time player.
        """
        key = str(real or "").strip()
        if not key:
            return ""
        with self._lock:
            record = self._record(key)
            handle = mint()
            record["slots"].append({
                "name": str(name or "").strip() or f"Profile {len(record['slots']) + 1}",
                "handle": handle,
                "created": datetime.now(timezone.utc).replace(
                    microsecond=0).isoformat(),
            })
            record["active"] = handle
            self._write(self.data)
            return handle

    def choose_profile(self, real: str, handle: str) -> bool:
        """Play as one of this account's existing profiles.

        Selecting never writes to a save, so switching away and back returns
        exactly what was there. The pointer is the only thing that moves, and
        it only ever moves to a profile that already exists.
        """
        key = str(real or "").strip()
        with self._lock:
            record = self._record(key)
            if not any(slot.get("handle") == handle for slot in record["slots"]):
                return False
            record["active"] = handle
            self._write(self.data)
            return True

    def rename_profile(self, real: str, handle: str, name: str) -> bool:
        text = str(name or "").strip()
        if not text:
            return False
        key = str(real or "").strip()
        with self._lock:
            record = self._record(key)
            for slot in record["slots"]:
                if slot.get("handle") == handle:
                    slot["name"] = text
                    self._write(self.data)
                    return True
        return False

    def _load(self) -> dict[str, Any]:
        try:
            stored = json.loads(self.path.read_text("utf-8"))
            if isinstance(stored, dict):
                stored.setdefault("players", {})
                return stored
        except (OSError, ValueError):
            pass
        data = {
            "players": {},
            "created": datetime.now(timezone.utc).replace(
                microsecond=0).isoformat(),
            "what this is": (
                "The names your saved progress is filed under on Phantom Abyss "
                "preservation servers. They replace your Steam ID, which is "
                "never sent to a server. Each account here can have several "
                "profiles -- separate saves, so you can start again without "
                "losing the one you have -- and `active` is the one in play. "
                "This file stays on this machine: keep it with your backups, "
                "because without it a server cannot tell you are the same "
                "player, and that is the whole point of it."
            ),
        }
        self._write(data)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self._stamp = self._mtime()

    # ----------------------------------------------------------- the swap

    def mask(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Rewrite a request so it carries the handle instead of the account.

        Returns the rewritten payload and the platform id it replaced, which
        the caller needs in order to put it back in the reply.
        """
        real = ""
        for key, value in payload.items():
            if str(key).lower() in OWN_IDENTITY and isinstance(value, str) and value:
                real = value
                break
        handle = self.handle_for(real)

        out: dict[str, Any] = {}
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in OWN_IDENTITY:
                out[key] = handle if handle else value
            elif lowered in CREDENTIALS:
                out[key] = ""
            elif lowered in THIRD_PARTIES:
                out[key] = [] if isinstance(value, list) else ""
            else:
                out[key] = value
        return out, real

    def unmask(self, result: Any, real: str) -> Any:
        """Put the player's own id back, so the game sees what it sent.

        Only the handle is swapped back, and only into fields that carried a
        platform id to begin with. The game's own numbering -- `userID` and
        `runID` -- is left alone: those are the server's, not Steam's.
        """
        if not real:
            return result
        handle = self.handle_for(real)
        if isinstance(result, dict):
            out = {}
            for key, value in result.items():
                if (str(key).lower() in OWN_IDENTITY
                        and isinstance(value, str) and value == handle):
                    out[key] = real
                else:
                    out[key] = self.unmask(value, real)
            return out
        if isinstance(result, list):
            return [self.unmask(v, real) for v in result]
        return result

    # ------------------------------------------------------------ moving in

    def adopt(self, state_dir: Path, real: str) -> bool:
        """Move a save filed under a platform id over to the handle.

        Without this, turning the swap on would look exactly like a wiped save:
        the server would go looking for a profile under a name it had never
        been told. Runs once, the first time we see which account this is.
        """
        if not real:
            return False
        handle = self.handle_for(real)
        profiles = Path(state_dir) / "state" / "profiles"
        old, new = profiles / f"{real}.json", profiles / f"{handle}.json"
        moved = False
        if old.exists() and not new.exists():
            old.replace(new)
            moved = True

        # The seed owner is recorded by platform id too, and a stale one would
        # stop the player's own captured login from reaching them: the store
        # compares the pin against the id the request arrived with, which is
        # now a handle. Both locations are checked -- the backend keeps its pin
        # beside state.json, the store beside the profiles.
        for owner in (Path(state_dir) / "state" / "seed_owner.txt",
                      profiles / "seed_owner.txt"):
            try:
                if owner.exists() and owner.read_text("utf-8").strip() == real:
                    owner.write_text(handle, encoding="utf-8")
                    moved = True
            except OSError:
                pass
        return moved
