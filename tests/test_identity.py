"""The player's Steam account is not the server's business.

The game signs in with a SteamID64 and a live Steam ticket. A preservation
server needs neither -- it needs something stable to file a save under. So the
client swaps the platform id for a handle minted here, and swaps it back in the
reply, and the server never learns a platform id at all.

The alternative, hashing the SteamID64, only looks equivalent. A SteamID64 is
7656119 followed by ten digits and the range in use is narrower still, so a host
holding hashes can hash every candidate and recover the list.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.identity import Identity  # noqa: E402

ME = "76561198000000042"
ALT = "76561198000000043"

REQUEST = {
    "userId": 0,
    "playerId": ME,
    "platform": "STEAM",
    "currentUsername": "Tomb Raider",
    "verificationToken": "a-live-steam-auth-ticket",
    "platformVerificationId": "0110000123",
    "friendPlatformIds": ["76561198000000001", "76561198000000002"],
    "clientDungeonVersion": 128,
}


def test_nothing_identifying_survives_the_swap(tmp_path: Path) -> None:
    masked, real = Identity(tmp_path).mask(REQUEST)
    body = json.dumps(masked)

    assert ME not in body, "the account"
    assert "a-live-steam-auth-ticket" not in body, "the credential"
    assert "0110000123" not in body, "the platform verification id"
    assert "76561198000000001" not in body, "somebody else's account"
    assert real == ME, "the caller still needs it to answer the game"


def test_what_is_not_identifying_is_left_alone(tmp_path: Path) -> None:
    """A rewrite that mangles the protocol is worse than no rewrite."""
    masked, _ = Identity(tmp_path).mask(REQUEST)
    assert masked["currentUsername"] == "Tomb Raider"
    assert masked["clientDungeonVersion"] == 128
    assert masked["platform"] == "STEAM"
    assert set(masked) == set(REQUEST), "no field appears or disappears"


def test_the_game_gets_its_own_account_back(tmp_path: Path) -> None:
    """The client compares what comes back against what it sent."""
    identity = Identity(tmp_path)
    masked, real = identity.mask(REQUEST)
    reply = {"playerId": masked["playerId"], "userID": 62842,
             "nested": {"playerId": masked["playerId"]}}

    answered = identity.unmask(reply, real)
    assert answered["playerId"] == ME
    assert answered["nested"]["playerId"] == ME
    assert answered["userID"] == 62842, "the game's own numbering is untouched"


def test_the_handle_does_not_move(tmp_path: Path) -> None:
    """It is the save. If it changed between sessions, progress would vanish."""
    first, _ = Identity(tmp_path).mask(REQUEST)
    second, _ = Identity(tmp_path).mask(REQUEST)
    assert first["playerId"] == second["playerId"]


def test_two_accounts_on_one_machine_stay_apart(tmp_path: Path) -> None:
    """One handle per account, not per machine.

    This project was tested with two Steam accounts on one PC. Keying by
    machine would have merged them into a single save.
    """
    identity = Identity(tmp_path)
    mine, _ = identity.mask(REQUEST)
    theirs, _ = identity.mask(dict(REQUEST, playerId=ALT))
    assert mine["playerId"] != theirs["playerId"]


def test_the_handle_cannot_be_walked_back_to_the_account(tmp_path: Path) -> None:
    """No derivation from the platform id, so there is nothing to enumerate."""
    a = Identity(tmp_path / "one").mask(REQUEST)[0]["playerId"]
    b = Identity(tmp_path / "two").mask(REQUEST)[0]["playerId"]
    assert a != b, "the same account on two machines shares no derivation"
    for handle in (a, b):
        assert ME not in handle
        assert handle.startswith("pa-")


def test_the_map_stays_on_this_machine(tmp_path: Path) -> None:
    """It exists so the player keeps their save, and it never travels."""
    identity = Identity(tmp_path)
    identity.mask(REQUEST)
    stored = json.loads((tmp_path / "identity.json").read_text("utf-8"))
    assert ME in stored["players"], "locally, this is exactly the point"
    assert stored["players"][ME]["active"].startswith("pa-")
    assert "a-live-steam-auth-ticket" not in json.dumps(stored)


def test_an_existing_save_moves_across_rather_than_vanishing(tmp_path: Path) -> None:
    """Turning this on must not read as a wiped save."""
    profiles = tmp_path / "state" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / f"{ME}.json").write_text(
        json.dumps({"reputation": 5763, "currentUsername": "Tomb Raider"}),
        encoding="utf-8")
    (tmp_path / "state" / "seed_owner.txt").write_text(ME, encoding="utf-8")

    identity = Identity(tmp_path)
    assert identity.adopt(tmp_path, ME) is True

    handle = identity.handle_for(ME)
    moved = json.loads((profiles / f"{handle}.json").read_text("utf-8"))
    assert moved["reputation"] == 5763
    assert not (profiles / f"{ME}.json").exists()
    # The pin follows, or the captured login stops reaching its owner.
    assert (tmp_path / "state" / "seed_owner.txt").read_text("utf-8") == handle


def test_adopting_twice_does_not_clobber_a_newer_save(tmp_path: Path) -> None:
    profiles = tmp_path / "state" / "profiles"
    profiles.mkdir(parents=True)
    identity = Identity(tmp_path)
    handle = identity.handle_for(ME)
    (profiles / f"{handle}.json").write_text('{"reputation": 99}', encoding="utf-8")
    (profiles / f"{ME}.json").write_text('{"reputation": 1}', encoding="utf-8")

    identity.adopt(tmp_path, ME)
    kept = json.loads((profiles / f"{handle}.json").read_text("utf-8"))
    assert kept["reputation"] == 99, "the handle's own save wins"


# ---------------------------------------------------------------- profiles


def test_a_new_profile_is_a_save_the_server_has_never_seen(tmp_path: Path) -> None:
    """What players have asked for since release: start again, keep the old one.

    A profile is a handle, so a new one is simply a name no server has on file.
    It answers with what it sends a first-time player, and the existing save is
    not touched at any point.
    """
    identity = Identity(tmp_path)
    original = identity.handle_for(ME)
    fresh = identity.create_profile(ME, "Fresh run")

    assert fresh != original
    assert identity.handle_for(ME) == fresh, "the new one is now in play"
    names = [p["name"] for p in identity.profiles_for(ME)]
    assert names == ["Main", "Fresh run"]


def test_switching_back_returns_the_original_save(tmp_path: Path) -> None:
    """Selecting moves a pointer and never writes to a save."""
    identity = Identity(tmp_path)
    original = identity.handle_for(ME)
    identity.create_profile(ME, "Fresh run")

    assert identity.choose_profile(ME, original) is True
    assert identity.handle_for(ME) == original

    reopened = Identity(tmp_path)
    assert reopened.handle_for(ME) == original, "and it survives a restart"


def test_a_profile_that_does_not_exist_is_never_selected(tmp_path: Path) -> None:
    """The pointer is the save, so it only ever moves somewhere real."""
    identity = Identity(tmp_path)
    original = identity.handle_for(ME)
    assert identity.choose_profile(ME, "pa-madeup") is False
    assert identity.handle_for(ME) == original


def test_profiles_are_per_account(tmp_path: Path) -> None:
    identity = Identity(tmp_path)
    identity.create_profile(ME, "Fresh run")
    assert len(identity.profiles_for(ME)) == 2
    assert len(identity.profiles_for(ALT)) == 1, "the other account is untouched"


def test_an_older_identity_file_gains_profiles_without_moving_a_save(
    tmp_path: Path,
) -> None:
    """Upgrading must not look like a reset to somebody mid-playthrough."""
    handle = "pa-c6c15b2d95060c062600c3d926a6ee26"
    (tmp_path / "identity.json").write_text(
        json.dumps({"players": {ME: handle}}), encoding="utf-8")

    identity = Identity(tmp_path)
    assert identity.handle_for(ME) == handle
    assert identity.profiles_for(ME) == [
        {"name": "Main", "handle": handle, "active": True}
    ]


def test_renaming_leaves_the_save_where_it_is(tmp_path: Path) -> None:
    identity = Identity(tmp_path)
    handle = identity.handle_for(ME)
    assert identity.rename_profile(ME, handle, "Main run") is True
    assert identity.handle_for(ME) == handle
    assert identity.profiles_for(ME)[0]["name"] == "Main run"


def test_a_profile_chosen_elsewhere_is_picked_up(tmp_path: Path) -> None:
    """The window writes the choice; a running server must not miss it.

    Caught by switching to another profile against a running server and
    watching it keep serving the old one. From the outside that is
    indistinguishable from the switch being broken.
    """
    serving = Identity(tmp_path)
    original = serving.handle_for(ME)

    window = Identity(tmp_path)
    fresh = window.create_profile(ME, "Fresh run")
    assert fresh != original

    assert serving.handle_for(ME) == fresh, "the running server followed"
    window.choose_profile(ME, original)
    assert serving.handle_for(ME) == original, "and follows a switch back"


def test_an_unreadable_file_does_not_hand_out_a_new_handle(tmp_path: Path) -> None:
    """The handle is the save. Losing it quietly is the worst failure here.

    Re-reading identity.json on every request is what lets a running server
    follow a profile switch, and it puts a half-written or briefly locked file
    in the path of every request. Reading one as empty would mint a fresh
    handle and file the player's progress under a name no server has seen.
    """
    identity = Identity(tmp_path)
    handle = identity.handle_for(ME)

    identity.path.write_text("{ this is not json", encoding="utf-8")
    assert identity.handle_for(ME) == handle, "kept what it already had"

    identity.path.write_text("null", encoding="utf-8")
    assert identity.handle_for(ME) == handle, "and a valid file that says nothing"


def test_the_handle_is_swapped_back_wherever_it_appears(tmp_path: Path) -> None:
    """Not just in fields somebody thought to list.

    The login the real server sends names the player as their own sharer, and
    ours left that empty -- the client ended up with no sharer code at all,
    and asking the game instance for one froze it. Filling it in then shipped
    the handle to the game instead, because the swap back only covered a named
    list of fields and sharerID was not on it.
    """
    identity = Identity(tmp_path)
    handle = identity.handle_for(ME)

    reply = identity.unmask({
        "sharerID": handle,
        "playerId": handle,
        "nested": {"whatever": handle},
        "listed": [handle, "somebody-else"],
        "userID": 404886,
        "someoneElsesCode": "not-our-handle",
    }, ME)

    assert reply["sharerID"] == ME, "the field nobody had listed"
    assert reply["playerId"] == ME
    assert reply["nested"]["whatever"] == ME
    assert reply["listed"] == [ME, "somebody-else"]
    assert reply["userID"] == 404886, "the server's own numbering is not ours"
    assert reply["someoneElsesCode"] == "not-our-handle", "and not everyone's"
    assert handle not in json.dumps(reply), "nothing of ours goes out"
