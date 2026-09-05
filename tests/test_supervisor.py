"""The supervisor must never leave the game pointing at a server that is gone.

Patching rewrites ten URL strings inside the executable. A patched game with
nothing behind it cannot reach anything, and a player who launches from Steam
the next day gets a broken game and no explanation. So the patch is given back
on the way out -- normally, on a crash, on Ctrl-C -- and, because none of those
can be relied on, its state is checked again on the way in.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phantom_offline.supervisor as sup_mod  # noqa: E402
from phantom_offline import patch  # noqa: E402
from phantom_offline.supervisor import HOST, LISTEN, MODES, Supervisor  # noqa: E402


class FakeExe:
    """Stands in for the game executable, tracking patch state."""

    def __init__(self, tmp_path: Path):
        self.path = tmp_path / "PhantomAbyss-Win64-Shipping.exe"
        self.path.write_bytes(b"original")
        self.patched = False
        self.restored = 0
        self.applied = 0


def _wire(monkeypatch, fake: FakeExe, *, ports_free=True):
    """Point the supervisor at a fake game instead of the real install."""
    monkeypatch.setattr(sup_mod.client_config, "find_game", lambda: fake.path.parent)
    monkeypatch.setattr(
        sup_mod.client_config, "SHIPPING_EXE", Path(fake.path.name)
    )
    monkeypatch.setattr(sup_mod.patch, "is_patched", lambda exe: fake.patched)

    def apply(exe, *, backup_dir, port, dry_run=False):
        fake.patched = True
        fake.applied += 1
        return 10, backup_dir / "backup.exe"

    def restore(exe, *, backup_dir):
        fake.patched = False
        fake.restored += 1
        return True

    monkeypatch.setattr(sup_mod.patch, "apply", apply)
    monkeypatch.setattr(sup_mod.patch, "restore", restore)
    monkeypatch.setattr(sup_mod.server, "port_is_free", lambda p: ports_free)


def test_preflight_reports_a_missing_game(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sup_mod.client_config, "find_game", lambda: None)
    found = Supervisor(tmp_path).preflight()
    assert not found.ok
    assert "Phantom Abyss" in found.problems[0]


def test_preflight_refuses_a_port_already_in_use(tmp_path: Path, monkeypatch) -> None:
    """Usually means another copy is still running -- say so, do not fight it."""
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake, ports_free=False)
    found = Supervisor(tmp_path).preflight()
    assert not found.ok
    assert any("already in use" in p for p in found.problems)


def test_the_patch_is_given_back_when_the_game_exits(tmp_path: Path, monkeypatch) -> None:
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    sup = Supervisor(tmp_path / "state")
    monkeypatch.setattr(sup, "start_server", lambda mode: None)
    monkeypatch.setattr(sup, "launch_game", lambda exe, extra=None: _Exited())

    assert sup.run(HOST) == 0
    assert fake.patched is False, "the executable was left pointing at us"
    assert fake.restored == 1


def test_the_patch_is_given_back_when_the_game_crashes(tmp_path: Path, monkeypatch) -> None:
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    sup = Supervisor(tmp_path / "state")
    monkeypatch.setattr(sup, "start_server", lambda mode: None)

    def explode(exe, extra=None):
        raise RuntimeError("the game died on startup")

    monkeypatch.setattr(sup, "launch_game", explode)
    try:
        sup.run(HOST)
    except RuntimeError:
        pass
    assert fake.patched is False, "a crash must not strand the executable"


def test_a_stale_session_is_noticed(tmp_path: Path, monkeypatch) -> None:
    """The marker outlives a session that was killed rather than closed."""
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    state = tmp_path / "state"
    state.mkdir()
    (state / sup_mod.SESSION_MARKER).write_text("1", encoding="utf-8")

    found = Supervisor(state).preflight()
    assert found.stale_session is True


def test_a_game_left_patched_is_repaired_on_the_way_in(
    tmp_path: Path, monkeypatch
) -> None:
    """Running this again is the fix for "my game cannot connect any more".

    Adopting the existing patch would be cheaper, but it leaves a player whose
    game was stranded by a killed session with no obvious way out.
    """
    fake = FakeExe(tmp_path)
    fake.patched = True                      # a previous session was killed
    _wire(monkeypatch, fake)
    sup = Supervisor(tmp_path / "state")
    monkeypatch.setattr(sup, "start_server", lambda mode: None)
    monkeypatch.setattr(sup, "launch_game", lambda exe, extra=None: _Exited())

    sup.run(HOST)
    assert fake.restored == 2, "restored on the way in and again on the way out"
    assert fake.applied == 1, "and patched cleanly in between"
    assert fake.patched is False


def test_a_patched_game_with_no_backup_is_refused(tmp_path: Path, monkeypatch) -> None:
    """Patching over an unknown state would destroy the only original left."""
    import pytest

    fake = FakeExe(tmp_path)
    fake.patched = True
    _wire(monkeypatch, fake)
    monkeypatch.setattr(sup_mod.patch, "restore", lambda exe, *, backup_dir: False)
    sup = Supervisor(tmp_path / "state")
    monkeypatch.setattr(sup, "start_server", lambda mode: None)

    with pytest.raises(RuntimeError, match="no backup"):
        sup._take_patch(fake.path)


def test_the_marker_is_cleared_on_a_clean_exit(tmp_path: Path, monkeypatch) -> None:
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    state = tmp_path / "state"
    sup = Supervisor(state)
    monkeypatch.setattr(sup, "start_server", lambda mode: None)
    monkeypatch.setattr(sup, "launch_game", lambda exe, extra=None: _Exited())

    sup.run(HOST)
    assert not (state / sup_mod.SESSION_MARKER).exists()


def test_cleanup_is_safe_to_repeat(tmp_path: Path, monkeypatch) -> None:
    """atexit, the signal handler and the finally block all call it."""
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    sup = Supervisor(tmp_path / "state")
    sup._patched_by_us = True
    sup.cleanup(fake.path)
    sup.cleanup(fake.path)
    sup.cleanup(fake.path)
    assert fake.restored == 1, "restored more than once"


def test_the_two_modes_map_onto_the_server(tmp_path: Path) -> None:
    assert MODES[LISTEN] == "capture"
    assert MODES[HOST] == "offline"


def test_stopping_closes_the_sockets_and_waits_for_the_thread(
    tmp_path: Path,
) -> None:
    """`shutdown` only ends the loop -- it leaves the socket open.

    Stopping used to do just that, so the ports stayed claimed for the life of
    the process and the serving thread ran on alone. Neither shows while a
    session only ever happens once, and the window can start a second one.
    """
    sup = Supervisor(tmp_path, port=0, proxy_port=0)
    sup.start_server(HOST)
    listening = list(sup._servers)
    assert [s.fileno() for s in listening] != [-1, -1], "they are open"

    sup.stop_server()
    assert [s.fileno() for s in listening] == [-1, -1], "and closed after"
    assert sup._thread is None, "and nothing is still serving"


def test_an_unknown_mode_is_refused(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        Supervisor(tmp_path).run("something-else")


class _Exited:
    """A game process that has already finished."""

    def wait(self):
        return 0


def test_opening_the_window_puts_a_patched_game_back(
    tmp_path: Path, monkeypatch
) -> None:
    """The game should be vanilla whenever a session is not running.

    A session killed rather than closed leaves the executable pointing at a
    server that is not there, and the player has no way to know that is what
    happened -- their game simply stops connecting. Repairing when the window
    opens means that state cannot outlive the program that caused it.
    """
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    fake.patched = True

    supervisor = Supervisor(tmp_path / "state")
    message = supervisor.repair()

    assert "put your game back" in message
    assert fake.patched is False
    assert fake.restored == 1


def test_repair_says_nothing_when_there_is_nothing_to_mend(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    assert Supervisor(tmp_path / "state").repair() == ""
    assert fake.restored == 0


def test_repair_clears_a_session_that_never_shut_down(
    tmp_path: Path, monkeypatch
) -> None:
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    supervisor = Supervisor(tmp_path / "state")
    supervisor.marker.parent.mkdir(parents=True, exist_ok=True)
    supervisor.marker.write_text("1", encoding="utf-8")

    assert "did not shut down" in supervisor.repair()
    assert not supervisor.marker.exists()


def test_repair_says_so_when_it_cannot_put_the_game_back(
    tmp_path: Path, monkeypatch
) -> None:
    """A patched game with no backup is the one case a player must be told."""
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    fake.patched = True
    monkeypatch.setattr(sup_mod.patch, "restore", lambda exe, *, backup_dir: False)

    message = Supervisor(tmp_path / "state").repair()
    assert "Verify the files through Steam" in message


def test_a_session_reports_what_it_kept(tmp_path: Path) -> None:
    """Always said, even when all is well -- silence reads as not having looked."""
    said: list[str] = []
    supervisor = Supervisor(tmp_path / "state")
    supervisor.on_progress = said.append
    supervisor._check_what_was_kept()

    assert said, "the check must report either way"
    assert any("check" in line.lower() for line in said)


def test_cleanup_narrates_every_step(tmp_path: Path, monkeypatch) -> None:
    """A session that says nothing while it works looks like one that has hung.

    Closing the game after a capture left the window silent for a couple of
    minutes -- putting the executable back, finishing downloads and reading the
    archive all take real seconds, and none of them said so. Which step was
    slow could not be answered afterwards, because none of them had spoken.
    """
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    said: list[str] = []

    supervisor = Supervisor(tmp_path / "state")
    supervisor.on_progress = said.append
    supervisor.cleanup(fake.path)

    spoken = " ".join(said).lower()
    for expected in ("server", "game back", "checking"):
        assert expected in spoken, f"nothing said about {expected}: {said}"


def test_cleanup_still_only_runs_once(tmp_path: Path, monkeypatch) -> None:
    fake = FakeExe(tmp_path)
    _wire(monkeypatch, fake)
    fake.patched = True

    supervisor = Supervisor(tmp_path / "state")
    supervisor._patched_by_us = True
    supervisor.cleanup(fake.path)
    supervisor.cleanup(fake.path)
    assert fake.restored == 1, "a second cleanup must not restore again"


def test_a_substituted_backend_reaches_the_server(tmp_path: Path) -> None:
    """Reusing the patch-and-put-back part is the point.

    Anything built on top of this wants the game answered differently without
    reimplementing the one piece that must not be got wrong: giving the
    executable back on the way out, on a crash as much as a clean exit.
    """
    made = []

    def make(state_dir, library):
        made.append(state_dir)
        return sup_mod.server.OfflineBackend(state_dir / "state", archive=library)

    sup = Supervisor(tmp_path, port=0, proxy_port=0, make_backend=make)
    sup.start_server(HOST)
    try:
        assert made == [tmp_path], "asked for exactly one, and given the state dir"
    finally:
        sup.stop_server()
