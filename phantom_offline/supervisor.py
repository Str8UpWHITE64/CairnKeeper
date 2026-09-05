"""Owns a play session: the patch, the server, and the game.

Everything else in this project is a piece a person has to assemble. The
supervisor is what turns those pieces into "pick a mode and press play", and it
is the difference between a tool that works and one that other people can use.

It owns three lifecycles, and has to be right about all of them because the one
genuinely invasive thing this project does is rewrite ten URL strings inside the
game's executable:

    patch    verify, apply, restore -- always keeping the backup
    server   start in the chosen mode, stop when the game exits
    game     launch, and notice when it is gone

The rule that matters more than any other: **never leave the executable
patched**. A patched game with no server behind it cannot reach anything, and a
player who launches from Steam the next day gets a broken game and no idea why.
So the patch is restored on the way out, on a crash, on Ctrl-C, and -- because
none of those can be relied on -- checked again on the way in.
"""
from __future__ import annotations

import atexit
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import client_config, patch, reachability, server

# Written while we hold the patch, removed when we give it back. Its presence at
# startup means a previous session did not shut down cleanly.
SESSION_MARKER = "session.lock"

LISTEN = "listen"
HOST = "host"

JOIN = "join"

MODES = {
    LISTEN: server.MODE_CAPTURE,
    HOST: server.MODE_OFFLINE,
    JOIN: server.MODE_JOIN,
}


@dataclass
class Preflight:
    """What we found before touching anything."""

    game: Path | None = None
    exe: Path | None = None
    patched: bool = False
    stale_session: bool = False
    ports_free: bool = True
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class Supervisor:
    def __init__(
        self,
        state_dir: Path,
        *,
        port: int = 54908,
        proxy_port: int = 54909,
        verbose: bool = False,
        make_backend=None,
    ):
        # Passed through to server.build(). Something built on top of this one
        # -- a randomizer, say -- wants to answer the game differently while
        # still having the patch put back on the way out, which is the part of
        # this worth reusing rather than reimplementing.
        self.make_backend = make_backend
        self.state_dir = Path(state_dir)
        self._save_slots = None
        # Set when playing on somebody else's server. The game still talks to
        # this machine; this is where this machine passes it on to.
        self.remote = ""
        self.port = port
        self.proxy_port = proxy_port
        self.verbose = verbose
        self._servers: list[Any] = []
        self._thread: threading.Thread | None = None
        self._patched_by_us = False
        self._cleanup_done = False
        # Somewhere to send progress. Patching copies an 89 MB file twice, and
        # without a word about it the window looks frozen.
        self.on_progress = None

    # ------------------------------------------------------------ inspection

    @property
    def marker(self) -> Path:
        return self.state_dir / SESSION_MARKER

    def preflight(self) -> Preflight:
        """Look before leaping, and report in terms a player can act on."""
        found = Preflight()
        found.stale_session = self.marker.exists()

        found.game = client_config.find_game()
        if found.game is None:
            found.problems.append(
                "Could not find Phantom Abyss. Install it through Steam, or add "
                "its folder to KNOWN_GAME_PATHS in client_config.py."
            )
            return found

        found.exe = found.game / client_config.SHIPPING_EXE
        if not found.exe.exists():
            found.problems.append(f"The game is installed but {found.exe.name} is missing.")
            return found

        found.patched = patch.is_patched(found.exe)

        for port in (self.port, self.proxy_port):
            if not server.port_is_free(port):
                found.ports_free = False
                found.problems.append(
                    f"Port {port} is already in use. Another copy of this may "
                    "still be running."
                )
        return found

    # ---------------------------------------------------------------- patch

    def _say(self, message: str) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(message)
            except Exception:  # noqa: BLE001 - a listener must not stop a session
                pass
        else:
            print(f"  {message}")

    def _take_patch(self, exe: Path) -> None:
        """Point the game at us, from a known-good starting point.

        A session that was killed rather than closed leaves the executable
        patched. Adopting it would be cheaper -- re-applying copies 85 MB --
        but it also means a player whose game was left broken has no obvious
        way to fix it. Restoring first makes running this again the answer to
        "my game cannot connect any more", which is worth the copy.
        """
        if patch.is_patched(exe):
            self._say("The game was left pointing at us. Putting it back first.")
            if not patch.restore(exe, backup_dir=self.state_dir / "backup"):
                raise RuntimeError(
                    "The game is patched but there is no backup to restore "
                    "from. Verify the game files through Steam, then try again."
                )

        count, backup = patch.apply(
            exe, backup_dir=self.state_dir / "backup", port=self.port
        )
        if count == 0:
            raise RuntimeError(
                "Found no backend URLs to redirect. The game may have updated; "
                "the patch needs revisiting before it is safe to use."
            )
        self._patched_by_us = True
        self._say(f"Pointed the game at this computer ({count} addresses).")

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(str(int(time.time())), encoding="utf-8")

    def _give_back_patch(self, exe: Path | None) -> None:
        if not self._patched_by_us or exe is None:
            return
        try:
            if patch.restore(exe, backup_dir=self.state_dir / "backup"):
                self._say("Put your game back exactly as it was.")
            else:
                print(
                    "  WARNING: no backup found to restore from. Verify the "
                    "game files through Steam to put it back."
                )
        finally:
            self._patched_by_us = False
            self.marker.unlink(missing_ok=True)

    # --------------------------------------------------------------- server

    def start_server(self, mode: str) -> None:
        """Bring the local service up in the requested mode."""
        wire_mode = MODES[mode]
        direct, proxy, recorder = server.build(
            state_dir=self.state_dir,
            direct_port=self.port,
            proxy_port=self.proxy_port,
            verbose=self.verbose,
            mode=wire_mode,
            remote=self.remote,
            make_backend=self.make_backend,
        )
        if wire_mode == server.MODE_JOIN:
            self._say(f"Playing on {self.remote}. Your Steam account stays here.")
        self._servers = [direct, proxy]
        self.recorder = recorder
        self.backend = direct.backend

        # Progression lives in two places. The server half is above; the other
        # half is UserProgress.sav, which the client reads at startup without
        # asking anybody. A profile that swaps only the server half resets
        # nothing the player can see, so the local save travels with it.
        if wire_mode in (server.MODE_OFFLINE, server.MODE_JOIN):
            self._install_profile_save()

        # Progression from the last live session has to be applied before the
        # first request: profiles are cached once loaded, so a later update is
        # invisible until the next restart.
        if wire_mode == server.MODE_OFFLINE:
            try:
                carried = self.backend.sync_from_capture()
                if carried:
                    print(f"  carried your progress across for {len(carried)} player(s)")
            except Exception as exc:  # noqa: BLE001 - never block play
                print(f"  could not carry progress across: {exc}")

        self._thread = threading.Thread(
            target=server.serve_forever, args=(self._servers,), daemon=True
        )
        self._thread.start()

    def stop_server(self) -> None:
        """Stop the loops, wait for them, then give the ports back.

        `shutdown` only asks the loop to stop. Closing without waiting leaves a
        thread selecting on a handle that is gone, and not closing at all keeps
        the ports for the life of the process -- which a second session in the
        same window then cannot have.
        """
        for srv in self._servers:
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        for srv in self._servers:
            try:
                srv.server_close()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
        self._servers = []

    # ----------------------------------------------------------------- game

    def launch_game(self, exe: Path, extra_args: list[str] | None = None):
        """Start the game and hand back the process to wait on."""
        args = [str(exe), "-log", *(extra_args or [])]
        return subprocess.Popen(args, cwd=str(exe.parent))

    # ------------------------------------------------------------- lifetime

    def cleanup(self, exe: Path | None = None) -> None:
        """Give everything back. Safe to call more than once."""
        if self._cleanup_done:
            return
        self._cleanup_done = True
        # Narrated step by step, with timings. Putting the game back and
        # reading the archive take real seconds, and a session that says
        # nothing while it does them is indistinguishable from one that has
        # hung -- which is exactly how it looked the first time somebody
        # closed the game after a capture.
        for label, step in (
            ("Shutting the server down", self.stop_server),
            ("Putting your saved progress back", self._finish_profile_save),
            ("Putting your game back", lambda: self._give_back_patch(exe)),
            ("Checking what this session kept", self._check_what_was_kept),
        ):
            started = time.perf_counter()
            self._say(f"{label}…")
            step()
            spent = time.perf_counter() - started
            if spent > 3.0:
                self._say(f"  (that took {spent:.0f}s)")

    def _check_what_was_kept(self) -> None:
        """Read back what this session added, and say so either way.

        A truncated blob looks archived until the day it is needed, by which
        point there is nothing left to re-fetch it from. The full sweep of a
        real archive takes minutes, so it only reads what has changed since
        the last check -- a session's worth, a second or two -- and the result
        is always reported. Saying nothing when all is well is
        indistinguishable from not having looked.
        """
        try:
            from . import integrity

            result = integrity.check(
                self.state_dir, on_progress=lambda line: self._say(f"  {line}"))
            self._say(result.summary())
            for line in result.problems[:5]:
                self._say(f"  - {line}")
            if len(result.problems) > 5:
                self._say(f"  ... and {len(result.problems) - 5} more")
        except Exception as exc:  # noqa: BLE001 - never fail a session over this
            self._say(f"Could not check the archive: {exc}")

    def repair(self) -> str:
        """Put the game back, whether or not this program is what patched it.

        Called when the window opens rather than when a session starts, so the
        game spends all its time between sessions in the state Steam shipped
        it in. A session killed rather than closed leaves the executable
        pointing at a server that is not running, and the player has no way to
        know that is what happened -- their game simply stops connecting.
        """
        exe = None
        game = client_config.find_game()
        if game is not None:
            exe = game / client_config.SHIPPING_EXE
        if exe is None or not exe.exists():
            return ""

        mended = []
        if patch.is_patched(exe):
            if patch.restore(exe, backup_dir=self.state_dir / "backup"):
                mended.append("put your game back the way Steam installed it")
            else:
                return ("Your game is still pointed at this program and there "
                        "is no backup to restore. Verify the files through "
                        "Steam to put it back.")
        if self.marker.exists():
            self.marker.unlink(missing_ok=True)
            if not mended:
                mended.append("cleared a session that did not shut down")

        try:
            from .saves import SaveSlots

            recovered = SaveSlots(self.state_dir).recover()
            if recovered:
                mended.append(recovered)
        except Exception:  # noqa: BLE001
            pass
        return "; ".join(mended)

    def _slots(self):
        from .saves import SaveSlots

        if self._save_slots is None:
            self._save_slots = SaveSlots(self.state_dir)
        return self._save_slots

    def _active_profile(self) -> tuple[str, str]:
        """The chosen profile, and whose the save already on disk must be.

        The second is the account's first profile. Before profiles existed
        there was one save and nothing recording who it belonged to, so on the
        first session it has to be attributed to somebody -- and the only
        honest answer is the profile that was there before any others.
        """
        from .identity import Identity

        try:
            identity = Identity(self.state_dir)
            accounts = identity.accounts()
            if not accounts:
                return "", ""
            profiles = identity.profiles_for(accounts[0])
            first = profiles[0]["handle"] if profiles else ""
            return identity.handle_for(accounts[0]), first
        except Exception:  # noqa: BLE001 - never block play over this
            return "", ""

    def _install_profile_save(self) -> None:
        try:
            slots = self._slots()
            repaired = slots.recover()
            if repaired:
                self._say(f"Recovered: {repaired}.")
            handle, first = self._active_profile()
            if not handle:
                return
            note = slots.install(handle, presumed_owner=first)
            if note:
                self._say(note[0].upper() + note[1:] + ".")
        except Exception as exc:  # noqa: BLE001
            # Never take the game down over this, but never be quiet about it
            # either: the player needs to know their save was not swapped.
            self._say(f"Could not switch your saved progress: {exc}")

    def _finish_profile_save(self) -> None:
        try:
            if self._save_slots is None:
                return
            note = self._save_slots.finish()
            if note:
                self._say(note[0].upper() + note[1:] + ".")
        except Exception as exc:  # noqa: BLE001
            self._say(f"Could not put your saved progress back: {exc}")

    def run(self, mode: str, *, launch: bool = True, extra_args=None) -> int:
        """Patch, serve, play, and put everything back."""
        if mode not in MODES:
            raise ValueError(
                f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
        if mode == JOIN and not self.remote:
            raise ValueError("joining needs the address of a server to join")

        found = self.preflight()
        if found.stale_session:
            print(
                "  a previous session did not shut down cleanly; "
                "checking the game executable"
            )
        if not found.ok:
            for problem in found.problems:
                print(f"  {problem}")
            return 1

        exe = found.exe

        # Listening is pointless if nothing is there to listen to. Check before
        # patching, so a player whose connection is down is told plainly rather
        # than left staring at a game that cannot log in.
        if mode == LISTEN:
            reach = reachability.check()
            print(f"  {reach.message()}")
            if not reach.usable:
                print()
                print("  Nothing to listen to, so nothing would be kept.")
                print("  Run with --host to play offline instead.")
                return 2
            print()

        # Restore on the way out however we leave: normally, by exception, by
        # Ctrl-C, or by the process being told to stop. None of these is
        # sufficient alone, which is why the marker file exists as well.
        atexit.register(self.cleanup, exe)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.cleanup(exe))
            except (ValueError, OSError):
                pass  # not on the main thread, or unsupported here

        try:
            self._say("Preparing your game...")
            self._take_patch(exe)
            self._say("Starting the local server...")
            self.start_server(mode)

            if not launch:
                self._say("Server running. Not starting the game.")
                return 0

            self._say("Starting Phantom Abyss...")
            game = self.launch_game(exe, extra_args)
            self._say("Playing. Everything you touch is being kept."
                      if mode == LISTEN else "Playing offline.")
            game.wait()
            print("  the game has closed")

            # A live session is the only chance to compare what we would have
            # said against what the real service actually said. Do it while the
            # evidence is fresh, and keep the result where the player can look.
            if mode == LISTEN:
                self._check_fidelity()
            return 0
        except KeyboardInterrupt:
            print("\n  stopping")
            return 0
        finally:
            self.cleanup(exe)


    def _check_fidelity(self) -> None:
        """Compare our answers to the real ones, and keep the result."""
        from . import fidelity

        try:
            report = fidelity.compare(self.state_dir)
        except Exception as exc:  # noqa: BLE001 - never spoil a good session
            print(f"  could not check fidelity: {exc}")
            return
        if not report.checked:
            return
        print(f"  fidelity: {report.summary()}")
        path = fidelity.save(self.state_dir, report)
        if not report.clean:
            print(f"  the differences are listed in {path.name}")
            print("  it names fields and types only -- no accounts, no traffic")


def describe(found: Preflight) -> list[str]:
    """Preflight as lines a player can read."""
    lines = []
    lines.append(f"game        : {found.game or 'not found'}")
    if found.exe:
        lines.append(f"executable  : {found.exe.name}")
        lines.append(
            "patch       : "
            + ("already pointing at us" if found.patched else "original (untouched)")
        )
    if found.stale_session:
        lines.append("last session: did not shut down cleanly")
    lines.append("ports       : " + ("free" if found.ports_free else "IN USE"))
    return lines
