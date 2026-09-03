"""Whether the real service can be reached, said carefully.

This project exists because the service will eventually go away, so the client
has to notice when it does. It also has to be honest about the limits of what
it can tell, because the failure it is watching for looks exactly like a
router being rebooted.

Four states are distinguishable:

    healthy       answered, and said it is fine
    maintenance   answered, and said it is busy -- a 503 carrying the real
                  maintenance body, which is the service working, not failing
    unreachable   nothing answered
    refused       something answered, but not what we expect

Only "unreachable" is ambiguous, and that ambiguity is permanent: from one
machine, a service that has shut down forever is indistinguishable from one
that is up while the player's connection is down. So this never claims the
former. Telling somebody their game is dead when their wifi dropped would be
both wrong and, on the day it is finally right, indistinguishable from all the
times it was not.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

GAMESERVICE = "https://gameservice.wiby.net"

HEALTHY = "healthy"
MAINTENANCE = "maintenance"
UNREACHABLE = "unreachable"
REFUSED = "refused"


@dataclass
class Reachability:
    state: str
    detail: str = ""

    @property
    def usable(self) -> bool:
        """Whether listening is worth attempting."""
        return self.state in (HEALTHY, MAINTENANCE)

    def message(self) -> str:
        """What to tell the player. Never more than we know."""
        if self.state == HEALTHY:
            return "The Phantom Abyss servers are up."
        if self.state == MAINTENANCE:
            return (
                "The servers are up but busy with maintenance. "
                "You can usually still play; try again shortly if not."
            )
        if self.state == REFUSED:
            return (
                "The servers answered, but not in a way this understands. "
                f"({self.detail}) You can still play offline."
            )
        return (
            "Can't reach the Phantom Abyss servers right now. This is usually "
            "a connection problem at one end or the other. You can play "
            "offline in the meantime -- everything you have kept is there."
        )


def check(timeout: float = 8.0, url: str = GAMESERVICE) -> Reachability:
    """Ask the service how it is, without needing an account to do it.

    `/WakeUp` with an empty body is answered by the real service without
    credentials -- with a 503 and a maintenance body, which is exactly the
    signal wanted here. A reply of any shape proves the service is there.
    """
    request = urllib.request.Request(
        f"{url}/WakeUp",
        data=b"{}",
        headers={"Content-Type": "application/json",
                 "User-Agent": "X-UnrealEngine-Agent"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096)
        # Health is proven by a body we recognize, not by a 200. A captive
        # portal answers everything with 200 and a sign-in page, and treating
        # that as "the servers are up" would send a player into a game that
        # cannot log in.
        return _read_body(body, default=REFUSED)
    except urllib.error.HTTPError as exc:
        # An HTTP error still means something is listening and speaking to us.
        try:
            body = exc.read(4096)
        except Exception:  # noqa: BLE001
            body = b""
        return _read_body(body, default=REFUSED, status=exc.code)
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Reachability(UNREACHABLE, str(reason))


def _read_body(body: bytes, *, default: str, status: int | None = None) -> Reachability:
    """A maintenance body is the service working, not failing."""
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        parsed = None

    if isinstance(parsed, dict):
        # The shape the real service answers /WakeUp with, whatever the status.
        if "maintenanceTimeUTC" in parsed or "serverVersion" in parsed:
            if parsed.get("newGamesLockedOut"):
                return Reachability(MAINTENANCE, "new games are locked out")
            if status and status >= 500:
                return Reachability(MAINTENANCE, f"HTTP {status}")
            return Reachability(HEALTHY, f"HTTP {status or 200}")
        return Reachability(default, f"HTTP {status or 200}")

    return Reachability(default, f"HTTP {status or 200}, unrecognised body")
