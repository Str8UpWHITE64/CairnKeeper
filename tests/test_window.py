"""The window must survive being rebuilt.

It is rebuilt every time the player comes back from a session, and a failure
part-way through does not raise anything the player can see -- it just leaves
whatever had been packed so far. That looked like a broken program: the two
doors, and no Play button, no profile row and no footer beneath them.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

tk = pytest.importorskip("tkinter")


def _has_display() -> bool:
    try:
        root = tk.Tk()
    except tk.TclError:
        return False
    root.destroy()
    return True


HAS_DISPLAY = _has_display()


@pytest.fixture(autouse=True)
def _never_dial_out(monkeypatch):
    """The window asks the live service how it is. The tests must not.

    Opening the window starts that check on a background thread, so on a
    machine with the game installed every window test made a real request to
    wiby.net and left the thread running past the end of the test. One of them
    was still waiting on the socket when the interpreter shut down, and took
    the whole run with it.
    """
    from phantom_offline import reachability

    monkeypatch.setattr(
        reachability, "check",
        lambda *a, **k: reachability.Reachability(reachability.UNREACHABLE),
    )


def _open(tmp_path: Path):
    """The window, retried once if Tk could not read one of its own files.

    Windows loses a Tcl theme file for an instant now and then -- seen here
    and on a hosted runner, both times a file plainly sitting where the error
    says it is not. This is not a way around a TclError the program causes:
    that raises both times and still fails the test.
    """
    from phantom_offline.gui import App

    try:
        return App(tmp_path)
    except tk.TclError:
        return App(tmp_path)


@pytest.fixture
def app(tmp_path: Path):
    """A window per test.

    The skip covers the probe above and nothing else. Building the App is left
    unguarded on purpose: the fixture used to swallow a TclError from it, and a
    leftover callback from the previous test made the host case -- the one that
    was actually broken -- skip itself silently while the listen case passed. A
    skip that hides the failing case is worse than the failure.
    """
    if not HAS_DISPLAY:
        pytest.skip("no display")
    window = _open(tmp_path)
    window.root.update()
    yield window
    window.root.destroy()
    window.root.update()


@pytest.mark.parametrize("mode", ["listen", "host"])
def test_the_home_screen_rebuilds_whole(app, mode: str) -> None:
    """Coming back from a session rebuilds it, in whichever mode was played.

    Host was the broken one: `_build_home` destroys its children and then
    re-selects the mode, which reached for the profile row through an attribute
    still pointing at the widget that had just been destroyed.
    """
    app._select(mode)
    app.root.update()
    app._build_home()
    app.root.update()

    for name in ("play", "phantom_stat", "floor_stat", "foot_left"):
        widget = getattr(app, name, None)
        assert widget is not None and widget.winfo_exists(), f"{name} is missing"


def test_the_profile_row_follows_the_mode(app) -> None:
    """Listen talks to the real service, which has never heard of our profiles.

    Hiding it is the window agreeing with the rule the client already enforces:
    identities are only swapped in offline mode.
    """
    app._select("host")
    app.root.update()
    assert app.profile_row.winfo_ismapped()

    app._select("listen")
    app.root.update()
    assert not app.profile_row.winfo_ismapped()


def test_rebuilding_twice_is_fine(app) -> None:
    for _ in range(3):
        app._select("host")
        app._build_home()
        app.root.update()
    assert app.play.winfo_exists()


# ---------------------------------------------------------------- reporting


def _served(tmp_path: Path, *, archived: bool = True) -> None:
    """An archive where a temple has just been served to somebody."""
    import json

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "state.json").write_text(json.dumps({
        "lastServed": {"841573782": {
            "dungeonID": 701683, "dungeonFloorNumber": 0, "dungeonSeed": 5,
            "areaID": 1, "gameMode": 1, "dungeonSettingsIndex": 0,
            "dungeonLayoutType": 3, "dungeonFloorType": 1,
            "difficultyRating": 0, "routeID": 1, "routeStage": 0,
            "archived": archived,
        }}
    }), encoding="utf-8")


def test_a_player_can_report_a_temple_without_a_terminal(tmp_path: Path) -> None:
    """The only way to report one used to be the command line.

    Which meant, in practice, that nobody did: the people most likely to hit an
    unfinishable temple are the ones least likely to open a terminal to say so.
    """
    if not HAS_DISPLAY:
        pytest.skip("no display")
    from phantom_offline.blacklist import Blacklist, archived_id

    _served(tmp_path)
    app = _open(tmp_path)
    app.root.update()
    try:
        assert app.report_row.winfo_ismapped(), "the offer must be visible"
        assert "701683" in app.report_line.cget("text")
        assert app.report_link.cget("text")

        app.on_report()
        app.root.update()

        black = Blacklist(tmp_path / "state" / "blacklist.json")
        assert black.get(archived_id(701683, 0)) is None, "opening reports nothing"
    finally:
        app.root.destroy()
        app.root.update()


def test_a_temple_someone_else_reported_asks_for_agreement(tmp_path: Path) -> None:
    """The second report is a confirmation, not an accusation.

    Showing the player that somebody already hit this wall, right beside the
    button, is what turns one stranger's word into two people agreeing.
    """
    if not HAS_DISPLAY:
        pytest.skip("no display")
    from phantom_offline.blacklist import SOURCE_GUEST, Blacklist

    _served(tmp_path)
    black = Blacklist(tmp_path / "state" / "blacklist.json")
    black.report_archived(701683, 0, reason="missing-key",
                          player_id="somebody-else", source=SOURCE_GUEST)

    app = _open(tmp_path)
    app.root.update()
    try:
        line = app.report_line.cget("text")
        assert "Another player reported" in line
        assert "could not finish" in app.report_link.cget("text")
    finally:
        app.root.destroy()
        app.root.update()


def test_a_withheld_temple_says_so(tmp_path: Path) -> None:
    if not HAS_DISPLAY:
        pytest.skip("no display")
    from phantom_offline.blacklist import Blacklist

    _served(tmp_path)
    Blacklist(tmp_path / "state" / "blacklist.json").report_archived(
        701683, 0, reason="missing-key", player_id="me")

    app = _open(tmp_path)
    app.root.update()
    try:
        assert "withheld" in app.report_line.cget("text")
    finally:
        app.root.destroy()
        app.root.update()


def test_nothing_to_report_shows_nothing(tmp_path: Path) -> None:
    if not HAS_DISPLAY:
        pytest.skip("no display")
    app = _open(tmp_path)
    app.root.update()
    try:
        assert not app.report_row.winfo_ismapped()
    finally:
        app.root.destroy()
        app.root.update()


def test_the_window_is_named_for_the_project(app) -> None:
    assert app.root.title() == "CairnKeeper"


def test_every_door_gets_the_same_room(app) -> None:
    """A third door was added and the text under it ran off the edge.

    `pack` hands out leftover space in proportion to what each child asked
    for, so the longest heading took the widest door and the newest, wordiest
    one got the least. The blurb also wrapped to a fixed width chosen when
    there were two doors, so it was cut off rather than wrapped.
    """
    app.root.update()
    widths = []
    for door in app.doors.values():
        _edge, inner, _row, _name, text, _chosen, _flag = door.parts
        widths.append(inner.winfo_width())
        # Against the room inside the padding, not the frame's outer
        # width. Measuring the outer width is what let the text overflow
        # by exactly the padding and clip the last word of a line.
        room = inner.winfo_width() - 2 * int(str(inner.cget("padx")))
        assert text.cget("wraplength") <= room, "wraps too wide"
        assert text.winfo_reqwidth() <= room, "a blurb overflows its door"
    assert max(widths) - min(widths) <= 2, f"doors differ in width: {widths}"


def test_the_blurbs_fit_at_the_smallest_the_window_goes(app) -> None:
    """Whatever the player does to the window, the words have to be readable."""
    app.root.geometry("940x600")
    app.root.update()
    app.root.update()
    for key, door in app.doors.items():
        _edge, inner, _row, _name, text, _chosen, _flag = door.parts
        assert text.winfo_reqheight() <= inner.winfo_height(), \
            f"the {key} blurb does not fit"
