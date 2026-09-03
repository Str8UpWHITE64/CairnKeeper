"""Looks up players by share code and archives the routes they have shared.

How sharing works, from captured traffic
----------------------------------------
Every player has an 8-character ``userShareCode`` (e.g. ``endohifg``).
``/GetDungeonShareList`` takes that code -- in a field confusingly named
``requestedUserName`` -- and returns the owner plus their shared routes::

    REQ  {"requestedUserName": "afldbifg", "networkFilter": false, ...}
    RESP {"userExists": true, "sharerUserID": 62738, "username": "I0I0II000",
          "reputation": 6873, "sharedRoutes": [...]}

An individual shared route is a two-part string: base64 JSON describing the
route, then a 112-byte binary signature.

    {"sharePublicVersion": 2, "routeStage": 2, "relicIDs": [""],
     "dungeonKeys": null}

The signature means share codes cannot be forged, so this only works with codes
players actually published.

Why it matters: this is the one route to archiving temples nobody on this
machine has played. Everything else is bounded by your own play history.
"""
from __future__ import annotations

import base64
import json
import sys
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .assets import AssetArchiver
from .session import SessionStore
from .upstream import Forwarder, UpstreamError

ENDPOINT = "https://gameservice.wiby.net/GetDungeonShareList"


def safe(text: Any) -> str:
    """Printable form of a player-chosen name.

    Usernames are arbitrary Unicode, and a redirected stdout on Windows is
    cp1252 by default -- printing one directly killed a 114-code run at entry
    101. Undisplayable characters are replaced rather than raising.
    """
    value = "" if text is None else str(text)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return value.encode(encoding, errors="replace").decode(encoding, errors="replace")

# Both share codes observed so far ("afldbifg", "endohifg") are exactly 8
# lowercase alphanumerics, so that is the default. It is only two samples, so
# `length=None` widens the net when a list turns out to use another size --
# at the cost of matching ordinary words in pasted chat text.
CODE_LENGTH = 8

BACKOFF_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class ShareResult:
    code: str
    status: int = 0
    exists: bool = False
    username: str | None = None
    sharer_user_id: int | None = None
    routes: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def parse_codes(text: str, length: int | None = CODE_LENGTH) -> list[str]:
    """Pull share codes out of a code list or pasted chat text.

    Handles the common community format of one ``<code>-<player name>`` per
    line: only the part before the first dash is considered, so a player whose
    name happens to be eight characters is not mistaken for a code. Lines
    starting with ``#`` are comments.

    Falls back to scanning loose tokens when a line has no dash, which is what
    raw Discord pastes look like. A wrong guess costs one lookup and comes back
    ``userExists: false``, so over-matching is cheap but worth avoiding.
    """
    pattern = (
        re.compile(rf"^[a-z0-9]{{{length}}}$")
        if length
        else re.compile(r"^[a-z0-9]{6,12}$")
    )
    seen: list[str] = []

    def keep(candidate: str) -> None:
        code = candidate.strip().lower()
        if pattern.match(code) and code not in seen:
            seen.append(code)

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "-" in line:
            # "<code>-<name>": the name may itself contain dashes and spaces.
            keep(line.split("-", 1)[0])
            continue
        for token in re.split(r"[^A-Za-z0-9]+", line):
            keep(token)
    return seen


def decode_share(entry: Any) -> dict[str, Any] | None:
    """Decode the readable half of a share code.

    `sharedRoutes` entries are full RouteInfo objects whose `shareCode` field
    holds the code itself; a bare string is also accepted. The code is
    ``base64(json)`` followed by a comma and a binary signature, so only the
    first half is readable.
    """
    if isinstance(entry, dict):
        entry = entry.get("shareCode")
    if not isinstance(entry, str) or not entry:
        return None
    head = entry.partition(",")[0]
    try:
        return json.loads(base64.b64decode(head))
    except Exception:  # noqa: BLE001 - malformed shares are not fatal
        return None


def shared_dungeons(route: Any) -> list[dict[str, Any]]:
    """The dungeons a shared route points at.

    This is the payoff: specific dungeon ids somebody else played, with the
    real `numGhosts` count -- often far above the ~50 a single /GetDungeon
    response will hand back.
    """
    if not isinstance(route, dict):
        return []
    out = []
    for dungeon in route.get("dungeons") or []:
        if isinstance(dungeon, dict) and dungeon.get("dungeonID"):
            out.append(dungeon)
    return out


def routes_from_fixtures(fixtures: Path) -> list[dict[str, Any]]:
    """Every shared route recorded by previous `shares` lookups."""
    source = fixtures / "GetDungeonShareList.jsonl"
    if not source.exists():
        return []
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for line in source.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        body = entry.get("response_body")
        if entry.get("status") != 200 or not isinstance(body, dict):
            continue
        for route in body.get("sharedRoutes") or []:
            code = (route or {}).get("shareCode")
            if code and code not in seen:
                seen.add(code)
                out.append(route)
    return out


# Statuses meaning the problem is ours, not the individual share code, and that
# continuing would only repeat it. Everything else -- notably 500, a code that
# no longer resolves -- is skipped without counting toward the abort.
FATAL_STATUSES = {0, 401, 403, 429}


class ShareHarvester:
    """Archives the temples behind shared routes.

    ``shareCode`` is the only field the server honours when asking for a
    *specific* temple. A fresh route ignores ``dungeonId``, a finished route
    refuses re-requests, and a retry mints something new -- but a share code
    resolves to exactly the temple it names.

    This matters most for Abyss. The mode retires a temple once anybody beats
    it ("Only ONE person in the world will beat each temple"), so a fresh Abyss
    route can only ever hand out something nobody has cleared -- usually a
    brand new, empty one. Shared Abyss routes point at temples that players
    repeatedly failed, which is where the phantoms are.

    Two limits, both measured rather than assumed:

    * Roughly half of shared codes return **500**. Community reports say temple
      sharing was removed from the game at some point, which would explain
      codes that no longer resolve; temples having since been beaten would also
      fit. Either way the working half is what remains reachable.
    * A share code opens the route, not just its first temple. Carrying the
      route and dungeon ids from that response into later floors works, and a
      harvest of 55 live routes averaged three floors each -- so shared temples
      usually arrive complete enough to play. (An earlier note here claimed
      floor 0 only, from a sample taken when the harvest was aborting on its
      first few dead codes and never reached a working one.)

    The share code must also travel alone: sending it alongside a `routeId`
    returns 500.
    """

    def __init__(self, state_dir: Path, *, delay: float = 2.0, max_failures: int = 4):
        self.delay = delay
        self.max_failures = max_failures
        self.session = SessionStore(state_dir / "session")
        self.archiver = AssetArchiver(state_dir / "assets")
        self.forwarder = Forwarder(state_dir / "fixtures", archiver=self.archiver)
        self.consecutive_failures = 0

    def _request(
        self,
        creds,
        floor: int,
        mode: str,
        *,
        share_code: str = "",
        route_id: int = 0,
        dungeon_id: int = 0,
    ):
        # The share code opens the temple; after that the route and dungeon ids
        # from the response carry the remaining floors. Sending both together
        # returns 500.
        body = dict(creds)
        body.update(
            {
                "userId": creds.get("userId") or 0,
                "gameMode": mode,
                "dungeonSettingsIndex": 0,
                "routeId": route_id,
                "routeStage": 0,
                "dungeonFloorNumber": floor,
                "dungeonId": dungeon_id,
                "shareCode": share_code,
                "knownDungeons": [],
                "whipWagered": "Bamboo",
                "isRetry": False,
                "difficultyRating": 0,
                "curseLevel": 0,
                "networkFilter": False,
                "platformFriends": [],
            }
        )
        try:
            status, _, data = self.forwarder.fetch(
                method="POST",
                url="https://gameservice.wiby.net/GetDungeon",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "X-UnrealEngine-Agent",
                },
                body=json.dumps(body).encode(),
            )
        except UpstreamError:
            return 0, None
        if status != 200:
            return status, None
        try:
            return status, json.loads(data)
        except ValueError:
            return status, None

    def run(self, routes: list[dict[str, Any]], *, limit: int | None = None) -> dict:
        creds = self.session.load()
        if not creds or not self.session.is_complete():
            raise RuntimeError(
                "no usable credentials. Run `serve --capture`, start the game "
                "and reach the main menu, then try again."
            )

        MODE_STRINGS = {0: "DGM_CLASSIC", 1: "DGM_ADVENTURE", 3: "DGM_DAILY"}
        stats = {"routes": 0, "floors": 0, "failed": 0, "ghosts": 0}
        todo = routes[:limit] if limit else routes

        for index, route in enumerate(todo, 1):
            if self.consecutive_failures >= self.max_failures:
                print(f"\nstopping after {self.consecutive_failures} failures")
                break

            code = route.get("shareCode")
            mode = MODE_STRINGS.get(route.get("gameMode"), "DGM_CLASSIC")
            expected = shared_dungeons(route)
            floors = max((d.get("numFloors") or 1) for d in expected) if expected else 1

            got = 0
            route_id = 0
            dungeon_id = 0
            for floor in range(min(int(floors), 8)):
                if floor == 0:
                    status, page = self._request(
                        creds, floor, mode, share_code=code
                    )
                else:
                    status, page = self._request(
                        creds, floor, mode,
                        route_id=route_id, dungeon_id=dungeon_id,
                    )
                if status != 200 or not page:
                    stats["failed"] += 1
                    # A 500 on a share code is that code, not the service:
                    # roughly half no longer resolve, and long runs of them are
                    # normal. Only a systemic problem -- auth, rate limiting, a
                    # network fault -- should stop the harvest, or the first few
                    # dead codes end it before it starts.
                    if status in FATAL_STATUSES:
                        self.consecutive_failures += 1
                    else:
                        stats["dead"] = stats.get("dead", 0) + 1
                    break
                self.consecutive_failures = 0
                route_id = int(page.get("routeID") or 0)
                dungeon_id = int(page.get("dungeonID") or 0)
                got += 1
                stats["floors"] += 1
                stats["ghosts"] += len(page.get("ghostRuns") or [])
                time.sleep(self.delay)

            if got:
                stats["routes"] += 1
                first = expected[0] if expected else {}
                print(
                    f"[{index}/{len(todo)}] route {route.get('routeID')} "
                    f"({mode}, area {first.get('areaID')}): {got} floor(s)"
                )

        self.archiver.drain()
        return stats


class ShareLookup:
    def __init__(self, state_dir: Path, *, delay: float = 2.0, max_failures: int = 4):
        self.state_dir = state_dir
        self.delay = delay
        self.max_failures = max_failures
        self.session = SessionStore(state_dir / "session")
        self.archiver = AssetArchiver(state_dir / "assets")
        self.forwarder = Forwarder(state_dir / "fixtures", archiver=self.archiver)
        self.consecutive_failures = 0

    def lookup(self, code: str) -> ShareResult:
        creds = self.session.load()
        result = ShareResult(code=code)
        body = dict(creds or {})
        body.update(
            {
                "requestedUserName": code,
                "networkFilter": False,
                "userId": (creds or {}).get("userId") or 0,
            }
        )
        try:
            status, _, data = self.forwarder.fetch(
                method="POST",
                url=ENDPOINT,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "X-UnrealEngine-Agent",
                },
                body=json.dumps(body).encode(),
            )
        except UpstreamError as exc:
            result.error = str(exc)
            return result

        result.status = status
        if status != 200:
            return result
        try:
            payload = json.loads(data)
        except ValueError:
            result.error = "response was not JSON"
            return result

        result.exists = bool(payload.get("userExists"))
        result.username = payload.get("username")
        result.sharer_user_id = payload.get("sharerUserID")
        result.routes = list(payload.get("sharedRoutes") or [])
        return result

    def run(self, codes: list[str]) -> list[ShareResult]:
        if not self.session.is_complete():
            raise RuntimeError(
                "no usable credentials. Run `serve --capture`, start the game "
                "and reach the main menu, then try again."
            )

        results: list[ShareResult] = []
        for index, code in enumerate(codes, 1):
            if self.consecutive_failures >= self.max_failures:
                print(
                    f"\nstopping after {self.consecutive_failures} consecutive "
                    "failures."
                )
                break

            result = self.lookup(code)
            results.append(result)

            if result.status == 200:
                self.consecutive_failures = 0
                who = safe(result.username) or "?"
                note = f"{len(result.routes)} shared route(s)"
                if not result.exists:
                    note = "no such user"
                print(f"[{index}/{len(codes)}] {code}: {who} -- {note}")
            else:
                self.consecutive_failures += 1
                print(f"[{index}/{len(codes)}] {code}: status {result.status}")
                if result.status in BACKOFF_STATUSES:
                    wait = min(self.delay * (2**self.consecutive_failures), 60.0)
                    print(f"    backing off {wait:.0f}s")
                    time.sleep(wait)

            time.sleep(self.delay)

        self.archiver.drain()
        return results
