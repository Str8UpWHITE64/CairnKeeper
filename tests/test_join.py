"""Playing on somebody else's server.

The game cannot be pointed at a remote host. The patched URLs say 127.0.0.1,
and a URL rewritten inside an executable has to be the same length as the one
it replaced, so there is no room for an address. The local server therefore
stays exactly where the game expects it and passes everything on.

That turns out to be the right shape anyway: it is the only place the player's
platform id can be swapped for a handle before it leaves the machine, and the
only place a remote's asset links can be re-pointed at something the game can
certainly reach.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import urllib.request
from pathlib import Path
from typing import NamedTuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import server as srv  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

LAYOUT = base64.b64encode(
    json.dumps({"numWings": 2, "roomInfos": [{"roomIndex": 0}]}).encode()
).decode()
RECORDING = base64.b64encode(json.dumps({
    "startPos": {"x": 1}, "numRecordedFrames": 10, "compressedLocations": "AA",
    "userId": 322356, "playerId": "76561198000000777",
    "verificationToken": "SOMEBODY-ELSES-TICKET",
}).encode()).decode()

class Joined(NamedTuple):
    """What the fixture below hands a test: two archives and two ports.

    The ports were fixed numbers, which meant the suite could not run beside
    anything holding them -- including a second copy of itself, which cost
    three separate false failures before anybody worked out it was that.
    Asking for port 0 and reading back what the machine gave is the same test
    and collides with nothing.
    """

    host_root: Path
    player_root: Path
    host_port: int
    join_port: int


def _content(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    library = Library(root / "assets", root / "fx")
    for floor in range(2):
        blob = f"pool__dungeon-701683-floor-{floor}-layout-x"
        (root / "assets" / blob).write_text(LAYOUT, encoding="utf-8")
        library.record_local_temple(701683, floor, {
            "dungeonID": 701683, "dungeonFloorNumber": floor, "gameMode": 1,
            "areaID": 1, "dungeonSeed": 5, "dungeonLayoutType": 3,
            "dungeonFloorType": 1, "dungeonFloorTotalCount": 2,
        }, blob, origin="cdn")
        ghost = f"pool__dungeon-701683-floor-{floor}-runinfo-a"
        (root / "assets" / ghost).write_text(RECORDING, encoding="utf-8")
        library.record_local_run(701683, floor, ghost,
                                 {"userName": "Shrim", "userID": 322356,
                                  "runID": 900, "success": 1})


def _serving(server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop(server, thread: threading.Thread) -> None:
    """Ask the loop to stop before closing the socket underneath it.

    Closing a socket a `serve_forever` thread is still selecting on leaves the
    thread alive on a handle that is gone. Several of those still running at
    interpreter shutdown segfaulted the suite about one run in ten.
    """
    server.shutdown()
    thread.join(timeout=10)
    server.server_close()


@pytest.fixture
def joined(tmp_path: Path):
    """A server somebody hosts, and a player's own client pointed at it."""
    host_root = tmp_path / "host"
    _content(host_root)
    host_direct, host_proxy, _ = srv.build(
        state_dir=host_root, direct_port=0, proxy_port=0,
        mode=srv.MODE_OFFLINE)
    host_port = host_direct.server_address[1]
    host_thread = _serving(host_direct)

    player_root = tmp_path / "player"
    (player_root / "assets").mkdir(parents=True)
    joiner, joiner_proxy, _ = srv.build(
        state_dir=player_root, direct_port=0, proxy_port=0,
        mode=srv.MODE_JOIN, remote=f"http://127.0.0.1:{host_port}")
    join_port = joiner.server_address[1]
    join_thread = _serving(joiner)

    try:
        yield Joined(host_root, player_root, host_port, join_port)
    finally:
        _stop(host_direct, host_thread)
        _stop(joiner, join_thread)
        host_proxy.server_close()
        joiner_proxy.server_close()


def _post(port: int, path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as answer:
        return json.loads(answer.read())


def _play(port: int, player: str, floor: int = 0) -> dict:
    return _post(port, "/GetDungeon", {
        "playerId": player, "gameMode": "DGM_ADVENTURE", "routeId": 0,
        "routeStage": 0, "dungeonId": 0 if floor == 0 else 701683,
        "dungeonFloorNumber": floor,
    })


def test_a_joined_player_gets_the_hosts_temples(joined) -> None:
    served = _play(joined.join_port, "76561198000000042")
    assert served["dungeonID"] == 701683
    assert [g["userName"] for g in served["ghostRuns"]] == ["Shrim"]


def test_asset_links_point_at_this_machine(joined) -> None:
    """So a player needs one reachable host, not two.

    It also means the server does not have to know its own public address,
    which a server in a container generally does not.
    """
    served = _play(joined.join_port, "76561198000000042")
    assert f"127.0.0.1:{joined.join_port}" in served["layoutDownloadURL"]
    assert f"127.0.0.1:{joined.host_port}" not in json.dumps(served)


def test_the_asset_itself_comes_through(joined) -> None:
    served = _play(joined.join_port, "76561198000000042")
    with urllib.request.urlopen(served["layoutDownloadURL"], timeout=30) as answer:
        layout = answer.read()
    assert json.loads(base64.b64decode(layout))["roomInfos"]

    ghost = served["ghostRuns"][0]["downloadURL"]
    with urllib.request.urlopen(ghost, timeout=30) as answer:
        recording = answer.read()
    assert json.loads(base64.b64decode(recording))["numRecordedFrames"] == 10


def test_the_host_never_learns_the_players_account(joined) -> None:
    """The reason the swap happens here and not on the server."""
    _post(joined.join_port, "/VerifyUserID", {
        "userId": 0, "playerId": "76561198000000042", "platform": "STEAM",
        "currentUsername": "Tomb Raider", "verificationToken": "MY-STEAM-TICKET",
        "platformVerificationId": "0110000123",
        "friendPlatformIds": ["76561198000000888"],
        "clientDungeonVersion": 128, "clientProtocolVersion": 7,
    })

    everything = ""
    for path in joined.host_root.rglob("*"):
        if path.is_file() and "assets" not in path.parts:
            everything += path.read_text("utf-8", errors="replace")
        if path.is_file():
            everything += path.name

    assert "76561198000000042" not in everything, "the account"
    assert "MY-STEAM-TICKET" not in everything, "the credential"
    assert "0110000123" not in everything
    assert "76561198000000888" not in everything, "their friends"


def test_the_game_still_sees_its_own_account(joined) -> None:
    answer = _post(joined.join_port, "/VerifyUserID", {
        "userId": 0, "playerId": "76561198000000042", "platform": "STEAM",
        "currentUsername": "Tomb Raider", "verificationToken": "t",
        "platformVerificationId": "", "clientDungeonVersion": 128,
        "clientProtocolVersion": 7,
    })
    assert answer["playerId"] == "76561198000000042"


def test_a_run_made_on_the_hosts_server_is_kept_there(joined) -> None:
    served = _play(joined.join_port, "76561198000000042")
    _post(joined.join_port, "/SubmitRun", {
        "playerId": "76561198000000042", "dungeonId": served["dungeonID"],
        "dungeonFloorNumber": 0, "gameMode": "DGM_ADVENTURE",
        "success": 0, "lifetime": 42.0, "runData": RECORDING,
    })

    host_library = Library(joined.host_root / "assets",
                           joined.host_root / "fx")
    stored = host_library.temples[(701683, 0)]
    assert len(stored.ghosts) == 2, "theirs, and the one just made"

    mine = Library(joined.player_root / "assets",
                   joined.player_root / "fx")
    assert not mine.temples, "the player's own archive stays their own"


def test_a_server_that_is_not_there_does_not_hang_the_game(tmp_path: Path) -> None:
    """A refusal the client can act on beats a request that never returns."""
    root = tmp_path / "player"
    (root / "assets").mkdir(parents=True)
    joiner, proxy, _ = srv.build(
        state_dir=root, direct_port=0, proxy_port=0,
        mode=srv.MODE_JOIN, remote="http://127.0.0.1:9")
    port = joiner.server_address[1]
    thread = _serving(joiner)
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/GetDungeon",
            data=json.dumps({"playerId": "x", "dungeonId": 0}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as answer:
                assert answer.status == 503
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
    finally:
        _stop(joiner, thread)
        proxy.server_close()


# ------------------------------------------------------------- hosting


def test_the_server_can_listen_beyond_this_machine(tmp_path: Path) -> None:
    """A container that only listens to itself is not a server.

    The default stays loopback -- a player's own machine has no reason to
    accept connections from anywhere -- so hosting is something you choose.
    """
    root = tmp_path / "host"
    (root / "assets").mkdir(parents=True)
    wide, proxy, _ = srv.build(state_dir=root, direct_port=0,
                               proxy_port=0, mode=srv.MODE_OFFLINE,
                               host="0.0.0.0")
    try:
        assert wide.server_address[0] == "0.0.0.0"
    finally:
        wide.server_close()
        proxy.server_close()

    narrow, proxy2, _ = srv.build(state_dir=root, direct_port=0,
                                  proxy_port=0, mode=srv.MODE_OFFLINE)
    try:
        assert narrow.server_address[0] == "127.0.0.1", "the safe default"
    finally:
        narrow.server_close()
        proxy2.server_close()


def test_the_image_and_compose_file_agree(tmp_path: Path) -> None:
    """A hosting guide that does not match the program helps nobody."""
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "Dockerfile").read_text("utf-8")
    compose = (root / "docker-compose.yml").read_text("utf-8")

    # It must listen outside the container, or nothing can reach it.
    assert "--host" in dockerfile and "0.0.0.0" in dockerfile
    # The port the game's patched URLs use, and what a bare address assumes.
    assert "54908" in dockerfile and "54908:54908" in compose
    # The archive is a mount, so the image ships no game content.
    assert "/archive" in dockerfile and "/archive" in compose
    assert "PHANTOM_OFFLINE_STATE" in dockerfile


def test_an_unwritable_archive_is_explained_not_traced(tmp_path: Path) -> None:
    """The first thing that went wrong when this was actually hosted.

    The container ran as uid 10001, the mounted directory belonged to whoever
    made it, and the first write failed with a traceback about
    `/archive/capture` -- which says nothing about ownership to somebody who
    has just typed `docker compose up`.
    """
    from phantom_offline.__main__ import _writable

    blocked = tmp_path / "archive"
    blocked.write_text("a file where the directory should be", encoding="utf-8")

    trouble = _writable(blocked)
    assert trouble, "an unwritable archive must be reported"
    joined = "\n".join(trouble)
    assert "cannot write to" in joined
    assert "PHANTOM_UID" in joined, "and say how to fix it"
    assert "docker compose up" in joined


def test_a_writable_archive_says_nothing(tmp_path: Path) -> None:
    from phantom_offline.__main__ import _writable

    assert _writable(tmp_path / "fresh") == []
    assert (tmp_path / "fresh").is_dir(), "and creates it on the way"


def test_the_image_does_not_swallow_its_own_output() -> None:
    """A server that logs nothing is indistinguishable from one that is dead.

    Python buffers stdout when it is not a terminal, so a quiet server never
    fills the buffer and `docker logs` stays empty -- including the startup
    summary that says how much of an archive it actually found.
    """
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(
        "utf-8")
    assert "PYTHONUNBUFFERED=1" in dockerfile
