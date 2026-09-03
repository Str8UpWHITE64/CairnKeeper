"""HTTP front end for the offline backend.

Runs in two modes at once:

* **direct**   -- a plain HTTP server on port 54908, which is the address the
  shipping build already uses for ``EServerTarget::ST_Local``.
* **proxy**    -- an HTTP/HTTPS forward proxy. The game is pointed at it with
  ``-httpproxy=127.0.0.1:PORT``. Requests for ``*.wiby.net`` are answered by the
  offline backend (TLS is terminated with a generated certificate); everything
  else is refused rather than forwarded, so the game stays fully offline.

Unknown endpoints return ``{"serverStatus": 0}`` and are logged loudly, so an
unimplemented call shows up in the capture instead of hanging the client.
"""
from __future__ import annotations

import gzip
import json
import re
import socket
import ssl
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .assets import AssetArchiver
from .backend import OfflineBackend
from .capture import Recorder
from .certs import CertStore
from .identity import Identity
from .library import Library
from .session import SessionStore
from .upstream import Forwarder, UpstreamError

GAME_HOSTS = ("wiby.net",)

# How the proxy treats game traffic.
MODE_OFFLINE = "offline"  # answer everything locally; never touch the network
MODE_JOIN = "join"        # pass everything to somebody else's server
MODE_CAPTURE = "capture"  # forward to the real servers and archive responses

# The game fetches phantoms concurrently, so several handler threads log at
# once. Without a lock their lines interleave mid-string and the log becomes
# unreadable exactly when you most need it.
_PRINT_LOCK = threading.Lock()


def log(message: str) -> None:
    with _PRINT_LOCK:
        print(message, flush=True)


# Once the executable is patched, the game addresses us directly on localhost
# and the original hostname is gone from the request. Capture mode has to put it
# back, or it would forward to itself in a loop. The redirector and the game
# service are distinguished by path.
REDIRECTOR_UPSTREAM = "https://redirector.wiby.net"
GAMESERVICE_UPSTREAM = "https://gameservice.wiby.net"
REDIRECTOR_PATHS = ("/redirectv3.json",)


def strip_pad(path: str) -> str:
    """Remove the underscore padding segment the URL patch introduces.

    The patched base URL is padded to the original's exact length with a
    throwaway segment, so requests arrive as ``/______/VerifyUserID``.
    """
    segments = [s for s in path.split("/") if s]
    while segments and segments[0] and set(segments[0]) == {"_"}:
        segments.pop(0)
    return "/" + "/".join(segments) if segments else "/"


def upstream_url(url: str) -> str:
    """Map a local request back onto the real backend it stands in for."""
    parsed = urlparse(url)
    if not _is_local(parsed.hostname or ""):
        return url
    path = strip_pad(parsed.path or "/")
    base = (
        REDIRECTOR_UPSTREAM
        if path.lower() in REDIRECTOR_PATHS
        else GAMESERVICE_UPSTREAM
    )
    suffix = f"?{parsed.query}" if parsed.query else ""
    return f"{base}{path}{suffix}"


def _is_local(host: str) -> bool:
    return host.lower().split(":")[0] in ("localhost", "127.0.0.1", "::1")


class Router:
    """Maps request paths onto backend methods."""

    def __init__(self, backend: OfflineBackend, base_url: str):
        self.backend = backend
        self.base_url = base_url
        b = backend
        self.routes: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "/redirectv3.json": lambda r: b.redirect(r, self.base_url),
            "/wakeup": lambda r: b.wake_up(r, self.base_url),
            "/getmaintenancestatus": b.maintenance_status,
            "/verifyuserid": b.verify_user,
            "/refreshverification": b.refresh_verification,
            "/getdungeon": lambda r: b.get_dungeon(r, self.base_url),
            "/submitdungeonlayout": b.submit_layout,
            "/submitrun": b.submit_run,
            "/getdailydungeoninfo": b.daily_info,
            "/purchaseupgrade": b.purchase_upgrade,
            "/purchasetemporarypower": b.purchase_temporary_power,
            "/convertkeys": b.convert_keys,
            "/spendcurrency": b.spend_currency,
            "/getfriends": b.friends,
            "/addfriend": b.generic_ok,
            "/completetutorialdungeon": b.complete_tutorial,
            "/requestdatareset": b.generic_ok,
            "/debuggivekeys": b.generic_ok,
            "/debuggiveessence": b.generic_ok,
            "/getdungeonsharelist": b.generic_ok,
            "/verifysharecode": b.generic_ok,
            # Ours, not the game's. A player's own copy of this program calls
            # it to tell a server it does not own that a temple cannot be
            # finished. Named so it can never be mistaken for a game endpoint.
            "/offline/report": b.report_temple,
        }

    def dispatch(self, path: str, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        key = strip_pad(urlparse(path).path).lower().rstrip("/") or "/"
        handler = self.routes.get(key)
        if handler is None:
            return {"serverStatus": 0}, f"UNHANDLED:{key}"
        return handler(body), key


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CairnKeeper/1.0"

    # Injected by the server factory.
    router: Router
    recorder: Recorder
    certs: CertStore
    verbose: bool
    mode: str
    forwarder: Forwarder | None
    session: SessionStore | None
    identity: "Identity | None"
    state_dir: Path
    remote: str = ""          # the server being played on, in join mode

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        if self.verbose:
            log(f"[http] {fmt % args}")

    # ------------------------------------------------------------ verbs

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_CONNECT(self) -> None:
        """TLS interception for the proxy mode."""
        host, _, port_s = self.path.partition(":")
        port = int(port_s or 443)

        if not _is_game_host(host):
            self.send_error(403, "blocked (offline mode)")
            return

        self.send_response(200, "Connection Established")
        self.end_headers()

        cert_path, key_path = self.certs.cert_for(host)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_path), str(key_path))

        try:
            tls_conn = ctx.wrap_socket(self.connection, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            log(f"[proxy] TLS handshake failed for {host}: {exc}")
            return

        # Serve subsequent plaintext requests over the tunnel.
        inner = _TunnelHandler(
            tls_conn,
            (host, port),
            self.server,
            router=self.router,
            recorder=self.recorder,
            certs=self.certs,
            verbose=self.verbose,
            mode=self.mode,
            forwarder=self.forwarder,
            session=self.session,
            scheme="https",
            authority=host,
            # A request that arrives inside a TLS tunnel is the same request.
            # Leaving these off meant the swap applied to plain HTTP only, so
            # whether a player's Steam id reached a server came down to which
            # way their client happened to connect.
            identity=self.identity,
            state_dir=self.state_dir,
        )
        inner.run()

    # ---------------------------------------------------------- request

    def _handle(self, method: str) -> None:
        body = self._read_body()
        parsed = urlparse(self.path)
        # The patched base URL carries a padding segment, so the client sends a
        # Host like "127.0.0.1:54908/_____". Keep only the authority, otherwise
        # the reconstructed URL ends up with the padding twice.
        host = (self.headers.get("Host", "") or parsed.netloc).split("/")[0]

        # Absolute-form URL (classic proxy request) for a non-game host.
        if parsed.scheme and parsed.netloc and not _is_game_host(parsed.hostname or ""):
            self.send_error(403, "blocked (offline mode)")
            return

        url = self.path if parsed.scheme else f"http://{host}{self.path}"
        self._respond(method, url, body)

    def _read_body(self) -> bytes:
        # Tracks whether the body on the wire was gzipped and we unpacked it.
        # If so, the Content-Encoding header must not be forwarded alongside the
        # decompressed bytes, or the upstream rejects it with 415.
        self.body_was_decompressed = False

        length = self.headers.get("Content-Length")
        if length:
            try:
                data = self.rfile.read(int(length))
            except (ValueError, OSError):
                return b""
        elif self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = self._read_chunked()
        else:
            return b""

        if "gzip" in self.headers.get("Content-Encoding", "").lower():
            try:
                data = gzip.decompress(data)
                self.body_was_decompressed = True
            except (OSError, EOFError):
                # Leave the bytes and the header alone; let upstream decide.
                pass
        return data

    def _read_chunked(self) -> bytes:
        chunks = bytearray()
        while True:
            line = self.rfile.readline().strip()
            if not line:
                break
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                self.rfile.readline()
                break
            chunks += self.rfile.read(size)
            self.rfile.readline()
        return bytes(chunks)

    def _respond(self, method: str, url: str, body: bytes) -> None:
        path = urlparse(url).path

        if self.mode == MODE_CAPTURE and self.forwarder is not None:
            status, headers, encoded, handler = self._forward(method, url, body)
        elif self.mode == MODE_JOIN and self.forwarder is not None and self.remote:
            status, headers, encoded, handler = self._join(method, url, body)
        else:
            status, headers, encoded, handler = self._synthesize(method, url, body)

        if handler.startswith(("UNHANDLED", "ERROR", "UPSTREAM-FAIL", "JOIN-FAIL")):
            log(f"[!] {handler}: {method} {path}")
        elif self.verbose:
            log(f"[>] {method} {path} -> {status}")

        self.recorder.record(
            method=method,
            url=url,
            req_headers=dict(self.headers),
            req_body=body,
            status=status,
            resp_body=encoded,
            handler=handler,
        )

        self.send_response(status)
        self.send_header(
            "Content-Type", headers.get("Content-Type", "application/json")
        )
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.wfile.write(encoded)

    def _join(
        self, method: str, url: str, body: bytes
    ) -> tuple[int, dict[str, str], bytes, str]:
        """Play on somebody else's server.

        The game cannot be pointed at a remote host: the patched URLs say
        127.0.0.1 and a URL rewritten in place has to be the same length. So
        the local server stays where the game expects it and passes everything
        on, which is also the only place the player's identity can be swapped
        out before it leaves the machine.

        Asset links come back pointing at the remote, and are rewritten to
        point here instead. Two reasons: the remote does not have to know its
        own public address -- a server in a container generally does not -- and
        a player only needs one reachable host rather than two.
        """
        parsed = urlparse(url)
        path = strip_pad(parsed.path or "/")
        suffix = f"?{parsed.query}" if parsed.query else ""
        target = f"{self.remote.rstrip('/')}{path}{suffix}"

        real = ""
        if self.identity is not None and body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict):
                masked, real = self.identity.mask(payload)
                body = json.dumps(masked).encode("utf-8")

        headers_out = {
            key: value for key, value in dict(self.headers).items()
            if key.lower() not in ("host", "content-length", "accept-encoding")
        }
        headers_out["Content-Length"] = str(len(body))
        try:
            status, headers, data = self.forwarder.fetch(
                method=method, url=target, headers=headers_out, body=body
            )
        except UpstreamError as exc:
            log(f"[join] {self.remote} did not answer: {exc}")
            return (
                503,
                {"Content-Type": "application/json"},
                json.dumps({"serverStatus": 0}).encode("utf-8"),
                f"JOIN-FAIL:{path}",
            )

        if data and not path.startswith("/asset/"):
            data = self._bring_home(data, real)
        return status, headers, data, f"JOINED:{path}"

    def _bring_home(self, data: bytes, real: str) -> bytes:
        """Make a remote answer make sense on this machine.

        Asset links are re-pointed at this server, and the player's own account
        is put back where the handle was, so the game sees what it sent.
        """
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        if "/asset/" in text:
            text = re.sub(
                r'https?://[^"\s]*?(/asset/[^"\s]*)',
                lambda m: f"{self._local_base()}{m.group(1)}",
                text,
            )
        if self.identity is not None and real:
            try:
                text = json.dumps(self.identity.unmask(json.loads(text), real))
            except ValueError:
                pass
        return text.encode("utf-8")

    def _local_base(self) -> str:
        """Where the game should come back to for assets.

        Asked of the socket actually being listened on rather than assumed:
        the port is configurable, and guessing the default sent the game to a
        server that was not there.
        """
        authority = getattr(self, "_authority", "")
        if authority:
            return f"{self._scheme}://{authority}"
        try:
            port = self.server.server_address[1]
        except Exception:  # noqa: BLE001
            port = 54908
        return f"http://127.0.0.1:{port}"

    def _forward(
        self, method: str, url: str, body: bytes
    ) -> tuple[int, dict[str, str], bytes, str]:
        """Capture mode: ask the real server, archive what it says."""
        target = upstream_url(url)

        # Keep the live session token so the bulk archiver can reuse it. Stored
        # outside the shareable capture -- see phantom_offline/session.py.
        if self.session is not None and body:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = None
            if isinstance(parsed, dict) and self.session.remember(parsed):
                if not getattr(self, "_token_noted", False):
                    log("[session] captured live credentials for bulk archiving")
                    type(self)._token_noted = True

        headers_out = dict(self.headers)
        if getattr(self, "body_was_decompressed", False):
            # The body is now plain JSON; claiming gzip earns a 415.
            headers_out.pop("Content-Encoding", None)
        try:
            status, headers, data = self.forwarder.fetch(
                method=method, url=target, headers=headers_out, body=body
            )
            if status == 200:
                self._bank(urlparse(target).path, body, data)
            return status, headers, data, f"CAPTURED:{urlparse(target).path}"
        except UpstreamError as exc:
            log(f"[!] upstream failed, falling back to offline: {exc}")
            status, headers, data, handler = self._synthesize(method, url, body)
            return status, headers, data, f"UPSTREAM-FAIL:{handler}"

    def _bank(self, path: str, body: bytes, data: bytes) -> None:
        """Hand a captured exchange to the backend so it can keep what matters.

        Never let this break the session: the player is mid-run against the
        live service, and a bookkeeping failure must not cost them the request.
        """
        def parse(raw: bytes):
            try:
                value = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, AttributeError):
                return None
            return value if isinstance(value, dict) else None

        try:
            self.router.backend.observe_capture(path, parse(body), parse(data))
        except Exception as exc:  # noqa: BLE001 - never fail a live request
            log(f"[capture] could not bank this exchange: {exc}")

    def _serve_asset(self, name: str) -> tuple[int, dict[str, str], bytes, str] | None:
        """Serve an archived temple layout or phantom recording."""
        library = self.router.backend.archive
        if library is None:
            return None
        path = library.asset_path(name)
        if path is None:
            return 404, {"Content-Type": "text/plain"}, b"not archived", "ASSET-MISS"
        return (
            200,
            {"Content-Type": "text/json"},
            path.read_bytes(),
            f"ASSET:{name}",
        )

    def _synthesize(
        self, _method: str, url: str, body: bytes
    ) -> tuple[int, dict[str, str], bytes, str]:
        """Offline mode: answer from the local backend."""
        path = strip_pad(urlparse(url).path)
        if path.startswith("/asset/"):
            served = self._serve_asset(path[len("/asset/") :])
            if served is not None:
                return served

        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(payload, dict):
                payload = {"__value__": payload}
        except (ValueError, UnicodeDecodeError):
            payload = {}

        # Everything past this point is served by a backend that may not be
        # ours -- and even when it is, its files should not become a record of
        # who played. The platform id is swapped for a handle on the way in and
        # swapped back on the way out, so the game sees its own account and the
        # server never sees a Steam id at all.
        real = ""
        if self.identity is not None:
            payload, real = self.identity.mask(payload)
            if real and real not in getattr(type(self), "_adopted", frozenset()):
                # The first request tells us which account this machine is, so
                # it is the first moment a save filed under the old name can be
                # moved across. Without it, turning this on reads as a wipe.
                if self.identity.adopt(self.state_dir, real):
                    log("[identity] moved a save onto its handle")
                type(self)._adopted = frozenset(
                    getattr(type(self), "_adopted", frozenset())) | {real}

        try:
            result, handler = self.router.dispatch(url, payload)
        except Exception as exc:  # never let a bug hang the game
            log(f"[error] {url}: {exc!r}")
            result, handler = {"serverStatus": 0}, f"ERROR:{exc!r}"

        if self.identity is not None:
            result = self.identity.unmask(result, real)

        encoded = json.dumps(result).encode("utf-8")
        return 200, {"Content-Type": "application/json"}, encoded, handler


class _TunnelHandler(_Handler):
    """Handles plaintext requests inside an intercepted TLS tunnel."""

    def __init__(  # noqa: PLR0913 - mirrors BaseHTTPRequestHandler wiring
        self,
        conn: ssl.SSLSocket,
        addr: tuple[str, int],
        server: Any,
        *,
        router: Router,
        recorder: Recorder,
        certs: CertStore,
        verbose: bool,
        mode: str,
        forwarder: Forwarder | None,
        session: SessionStore | None,
        scheme: str,
        authority: str,
        identity: "Identity | None" = None,
        state_dir: Path | None = None,
    ) -> None:
        self.identity = identity
        self.state_dir = Path(state_dir) if state_dir else Path(".")
        self.session = session
        self.router = router
        self.recorder = recorder
        self.certs = certs
        self.verbose = verbose
        self.mode = mode
        self.forwarder = forwarder
        self._scheme = scheme
        self._authority = authority
        # Deliberately bypass BaseHTTPRequestHandler.__init__, which would
        # immediately start handling; we drive the loop ourselves in run().
        self.connection = conn
        self.rfile = conn.makefile("rb", -1)
        self.wfile = conn.makefile("wb", 0)
        self.client_address = addr
        self.server = server

    def run(self) -> None:
        try:
            self.close_connection = False
            self.handle_one_request()
            while not self.close_connection:
                self.handle_one_request()
        except (OSError, ssl.SSLError):
            pass
        finally:
            for stream in (self.wfile, self.rfile):
                try:
                    stream.close()
                except OSError:
                    pass
            try:
                self.connection.close()
            except OSError:
                pass

    def _handle(self, method: str) -> None:
        body = self._read_body()
        url = f"{self._scheme}://{self._authority}{self.path}"
        self._respond(method, url, body)


def _is_game_host(host: str) -> bool:
    host = host.lower().split(":")[0]
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    return any(host == d or host.endswith("." + d) for d in GAME_HOSTS)


def _make_server(
    port: int, handler_cls: type, host: str = "127.0.0.1"
) -> ThreadingHTTPServer:
    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):  # noqa: ANN001
            # Client disconnects are routine and not worth a traceback, but
            # anything else is a real bug -- swallowing it makes the server
            # look like "connection refused" with no explanation.
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionResetError, BrokenPipeError, ssl.SSLError)):
                return
            log(f"[error] unhandled exception serving {client_address}:")
            log(traceback.format_exc())

    return _Server((host, port), handler_cls)


def build(
    *,
    state_dir: Path,
    direct_port: int = 54908,
    proxy_port: int = 54909,
    verbose: bool = False,
    mode: str = MODE_OFFLINE,
    host: str = "127.0.0.1",
    remote: str = "",
) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer, Recorder]:
    recorder = Recorder(state_dir / "capture")
    certs = CertStore(state_dir / "certs")
    # Shared content the player has chosen to accept. Only in offline mode:
    # while capturing, the live service decides what they see.
    from . import bundles as _bundles

    extra = _bundles.enabled_paths(state_dir) if mode == MODE_OFFLINE else []
    library = Library(state_dir / "assets", state_dir / "fixtures", bundles=extra)
    backend = OfflineBackend(state_dir / "state", archive=library)
    router = Router(backend, f"http://127.0.0.1:{direct_port}")
    print(f"  archive           : {library.summary()}")

    # The handle this machine presents to a server. Only offline: while
    # capturing, the request goes to Steam's own service, which needs the real
    # account and a real ticket -- swapping those would simply fail to sign in.
    identity = (Identity(state_dir)
                if mode in (MODE_OFFLINE, MODE_JOIN) else None)
    if identity is not None:
        known = len(identity.data.get("players") or {})
        print(f"  identity          : {known} handle(s) held locally; "
              f"no platform id reaches a server")

    forwarder = None
    session = None
    if mode == MODE_JOIN:
        # No archiver: playing on somebody else's server means their content,
        # kept by them. What this machine keeps is its own runs, and those go
        # to them too.
        forwarder = Forwarder(state_dir / "fixtures", archiver=None)
    if mode == MODE_CAPTURE:
        archiver = AssetArchiver(state_dir / "assets")
        forwarder = Forwarder(state_dir / "fixtures", archiver=archiver)
        session = SessionStore(state_dir / "session")

    handler = type(
        "BoundHandler",
        (_Handler,),
        {
            "router": router,
            "recorder": recorder,
            "certs": certs,
            "verbose": verbose,
            "mode": mode,
            "forwarder": forwarder,
            "session": session,
            "identity": identity,
            "state_dir": Path(state_dir),
            "remote": remote,
        },
    )

    # Loopback unless asked otherwise. A player's own machine has no reason to
    # accept connections from anywhere else, and a server that quietly listens
    # to the network is not a default anybody should get without choosing it.
    direct = _make_server(direct_port, handler, host=host)
    proxy = _make_server(proxy_port, handler, host=host)
    # Expose the backend so the caller can act on it before serving starts --
    # carrying progression across, for one, which has to happen before the
    # first request or the profile cache hides it until the next restart.
    direct.backend = backend
    proxy.backend = backend
    return direct, proxy, recorder


def serve_forever(servers: list[ThreadingHTTPServer]) -> None:
    threads = [
        threading.Thread(target=s.serve_forever, daemon=True, name=f"srv-{i}")
        for i, s in enumerate(servers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def port_is_free(port: int) -> bool:
    """Whether we could listen on this port.

    Asks by trying to bind, which is the thing we actually need to know and
    answers instantly. Connecting instead took two seconds per port here --
    nothing refuses the connection, so it waits out the full timeout -- and the
    window called this on every click, freezing for four seconds each time.

    SO_REUSEADDR is deliberately not set: on Windows it permits binding a port
    somebody else is already listening on, which would turn this into a test
    that always says yes.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True
