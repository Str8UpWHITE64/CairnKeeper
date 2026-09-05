"""The server must actually construct.

Regression: a reordered assignment in build() raised UnboundLocalError, and
because the server runs detached the failure was invisible -- it looked like
"connection refused" instead of a crash.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import server  # noqa: E402


@pytest.mark.parametrize("mode", [server.MODE_OFFLINE, server.MODE_CAPTURE])
def test_build_succeeds(tmp_path: Path, mode: str) -> None:
    direct, proxy, recorder = server.build(
        state_dir=tmp_path, direct_port=0, proxy_port=0, mode=mode
    )
    try:
        assert direct.socket is not None
        assert proxy.socket is not None
        assert recorder is not None
    finally:
        direct.server_close()
        proxy.server_close()


def test_offline_mode_has_no_forwarder(tmp_path: Path) -> None:
    direct, proxy, _ = server.build(
        state_dir=tmp_path, direct_port=0, proxy_port=0, mode=server.MODE_OFFLINE
    )
    try:
        assert direct.RequestHandlerClass.forwarder is None
    finally:
        direct.server_close()
        proxy.server_close()


def test_capture_mode_has_forwarder(tmp_path: Path) -> None:
    direct, proxy, _ = server.build(
        state_dir=tmp_path, direct_port=0, proxy_port=0, mode=server.MODE_CAPTURE
    )
    try:
        assert direct.RequestHandlerClass.forwarder is not None
    finally:
        direct.server_close()
        proxy.server_close()


def test_router_routes_known_endpoints(tmp_path: Path) -> None:
    direct, proxy, _ = server.build(state_dir=tmp_path, direct_port=0, proxy_port=0)
    try:
        router = direct.RequestHandlerClass.router
        for endpoint in ("/getdungeon", "/wakeup", "/verifyuserid", "/submitrun"):
            assert endpoint in router.routes
        # Padded paths must resolve too.
        _, handler = router.dispatch("/_____/WakeUp", {})
        assert handler == "/wakeup"
    finally:
        direct.server_close()
        proxy.server_close()


def test_the_tunnel_handler_is_given_the_same_identity(tmp_path: Path) -> None:
    """A request inside a TLS tunnel is the same request.

    The swap that keeps a platform id off a server was wired into the plain
    handler only, and the tunnel handler defaulted it away -- so whether a
    player's Steam id reached a server came down to which way their client
    happened to connect. Both paths reach the same `_synthesize`, so both need
    the same identity.
    """
    import inspect

    from phantom_offline import server as srv

    direct, proxy, _ = srv.build(
        state_dir=tmp_path, direct_port=0, proxy_port=0, mode=srv.MODE_OFFLINE
    )
    try:
        handler = direct.RequestHandlerClass
        assert handler.identity is not None, "offline mode swaps identities"
        assert handler.state_dir == tmp_path

        # And the tunnel is handed them rather than falling back to a default.
        source = inspect.getsource(srv._Handler)
        made = source.split("_TunnelHandler(", 1)[1].split("inner.run()", 1)[0]
        assert "identity=self.identity" in made
        assert "state_dir=self.state_dir" in made
    finally:
        direct.server_close()
        proxy.server_close()


def test_capture_mode_never_swaps_the_identity(tmp_path: Path) -> None:
    """Listen talks to Steam's own service, which needs the real account."""
    from phantom_offline import server as srv

    direct, proxy, _ = srv.build(
        state_dir=tmp_path, direct_port=0, proxy_port=0, mode=srv.MODE_CAPTURE
    )
    try:
        assert direct.RequestHandlerClass.identity is None
    finally:
        direct.server_close()
        proxy.server_close()


def test_something_else_can_answer_the_game(tmp_path: Path) -> None:
    """The seam other projects build on.

    A randomizer, a difficulty mod, anything that wants the game answered
    differently: it gets the state directory and the assembled archive and
    hands back whatever will play the backend's part. A factory rather than a
    finished object because the archive is put together inside build(),
    bundles included, and a caller cannot reproduce that without copying it.
    """
    seen = {}

    def make(state_dir, library):
        seen["state_dir"] = state_dir
        seen["archive"] = library
        return server.OfflineBackend(state_dir / "state", archive=library)

    direct, proxy, _ = server.build(
        state_dir=tmp_path, direct_port=0, proxy_port=0,
        mode=server.MODE_OFFLINE, make_backend=make,
    )
    try:
        assert seen["state_dir"] == tmp_path
        assert seen["archive"] is not None, "handed the archive, not left to find it"
        assert direct.backend is proxy.backend, "both servers answer the same way"
    finally:
        direct.server_close()
        proxy.server_close()


def test_capture_refuses_a_substituted_backend(tmp_path: Path) -> None:
    """Capture exists to record what the live service actually said.

    Anything else answering in that mode could archive a reply the service
    never gave, and every fixture, fidelity check and offline answer is built
    on those recordings being true.
    """
    with pytest.raises(ValueError, match="capture mode"):
        server.build(
            state_dir=tmp_path, direct_port=0, proxy_port=0,
            mode=server.MODE_CAPTURE, make_backend=lambda s, l: None,
        )
