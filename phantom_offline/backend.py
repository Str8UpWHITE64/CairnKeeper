"""The offline replacement for gameservice.wiby.net.

Design notes
------------
The client generates temples itself (Dungeon Architect, driven by
``dungeonSeed``). This backend therefore does not need to understand level
generation at all -- it needs to hand out a stable seed per temple and remember
the layout the client uploads, so repeat visits to the same temple are
identical.

Everything is stored as plain JSON under the state directory so a player's
offline progress is inspectable and portable.

Field names come from ``docs/PROTOCOL.md`` (recovered from UE reflection data).
Unreal's JSON deserialisation ignores unknown fields and leaves missing ones at
their default, so being a superset of what the client wants is safe.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .library import Library

from .blacklist import (
    IMPORT_THRESHOLD,
    REASONS,
    SOURCE_GUEST,
    Blacklist,
    Fingerprint,
    unblocked_seed,
)
from .profile import ProfileStore

# Confirmed from live capture (2026-08-26). The client sends
# clientDungeonVersion/clientProtocolVersion and the real server answered with
# matching serverVersion/serverProtocolVersion, so these must agree or the
# client is likely to reject us as out of date.
SERVER_VERSION = 128
SERVER_PROTOCOL_VERSION = 7

# Confirmed: a successful HTTP 200 /GetDungeon carried mode=1, so 1 is the
# healthy value. (An earlier guess of 0 was wrong -- 503 responses also carry
# mode=1, meaning `mode` is not the health signal at all.)
MAINTENANCE_MODE_OK = 1

# (dungeonLayoutType, dungeonFloorType) for a floor. These are not decorative:
# handing the client a combination that does not occur at that position makes
# its generator produce an empty temple -- numWings 0, no rooms -- and the game
# hangs on a black screen waiting for geometry that never arrives.
#
# Measured across 600+ real temples:
#
#   floorType is positional      first floor 1, last floor 3, middle floors 4
#   layoutType depends on mode   Adventure 3 (2 on the last floor of three)
#                                Abyss     1 throughout
ADVENTURE_SHAPES = {0: (3, 1), 1: (3, 4), 2: (2, 3)}
ABYSS_LAYOUT_TYPE = 1
FIRST_FLOOR_TYPE, LAST_FLOOR_TYPE, MIDDLE_FLOOR_TYPE = 1, 3, 4

# Floors per area for an Abyss route, indexed by difficulty then by area.
#
# The count is NOT uniform across a route. Unchained advertises 13 floors over
# four areas, which no single per-area number can produce -- an earlier model
# stored one count per difficulty and made Unchained 16 and Insane 12.
#
#   Master    (0)  2,2,2,2 =  8   all four areas observed
#   Insane    (1)  2,2,3,3 = 10   all four areas observed
#   Nightmare (2)  3,3,3,3 = 12   confirmed from a full captured route
#   Unchained (3)  3,3,3,4 = 13   areas 1-3 measured at 3 each = 9 of an
#                                 advertised 13, so the Rift is 4 by deduction
ABYSS_FLOORS_BY_AREA = {
    0: (2, 2, 2, 2),
    1: (2, 2, 3, 3),
    2: (3, 3, 3, 3),
    3: (3, 3, 3, 4),
}
ABYSS_AREAS = 4


def abyss_floors(difficulty: int, area_index: int) -> int:
    """How many floors this area of an Abyss route has.

    ``area_index`` is the route stage, 0-3 for Ruins/Caverns/Inferno/Rift.
    """
    layout = ABYSS_FLOORS_BY_AREA.get(int(difficulty or 0))
    if not layout:
        return 2
    if 0 <= area_index < len(layout):
        return layout[area_index]
    return layout[-1]

# How far down the temple rotation to look for one that is not blacklisted
# before falling back to generating a fresh temple.
MAX_ROTATION_SKIPS = 12


def floor_shape(floor: int, *, total: int = 3, abyss: bool = False) -> tuple[int, int]:
    """The (layoutType, floorType) the generator will accept at this position."""
    if abyss:
        if floor == 0:
            floor_type = FIRST_FLOOR_TYPE
        elif floor >= total - 1:
            floor_type = LAST_FLOOR_TYPE
        else:
            floor_type = MIDDLE_FLOOR_TYPE
        return ABYSS_LAYOUT_TYPE, floor_type
    if floor in ADVENTURE_SHAPES and total == 3:
        return ADVENTURE_SHAPES[floor]
    if floor == 0:
        return 3, FIRST_FLOOR_TYPE
    if floor >= total - 1:
        return 2 if total == 3 else 3, LAST_FLOOR_TYPE
    return 3, MIDDLE_FLOOR_TYPE


# `success` on /SubmitRun, from captured traffic.
RUN_FLOOR_CLEARED = 1  # mid-route; server banks nothing and returns currency null
RUN_DIED = 0  # route over
RUN_TEMPLE_COMPLETED = 2  # temple beaten; this is when a relic is reported

# The request names the mode as a string; the response echoes it as an int.
# Order read from the EDungeonGameMode enum in the binary, and confirmed by
# captured traffic: CLASSIC->0 (18x), ADVENTURE->1 (99x), DAILY->3 (3x).
# Note PRACTISE sits at 2, so DAILY is 3 and not 2 as a naive reading assumes.
#
# The menu names differ from the protocol names: DGM_CLASSIC is "Abyss Mode".
GAME_MODES = {
    "DGM_CLASSIC": 0,
    "DGM_ADVENTURE": 1,
    "DGM_PRACTISE": 2,
    "DGM_DAILY": 3,
    "DGM_ERRORMODE": 4,
}
MODE_NAMES = {0: "Abyss", 1: "Adventure", 2: "Practise", 3: "Daily"}


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _ue_time(when: datetime) -> str:
    """Format matching the binary's `%Y%m%dT%H%M%S%sZ` timestamp pattern."""
    return when.strftime("%Y%m%dT%H%M%SZ")


# How many temples per player this server remembers handing out. Enough for a
# player to report the floor that stopped them a couple of floors later, and
# far too few to be a record of what anybody played.
SERVED_HISTORY = 5


def _same_temple(a: Any, b: Any) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return (a.get("dungeonID") == b.get("dungeonID")
            and a.get("dungeonFloorNumber") == b.get("dungeonFloorNumber"))


# 7656119 plus ten digits: the shape of every SteamID64.
_LOOKS_LIKE_A_STEAM_ID = re.compile(r"^7656119\d{10}$")


def _first(data: dict[str, Any], *names: str) -> Any:
    """First present, non-None value among *names* (field casing varies)."""
    for name in names:
        if data.get(name) is not None:
            return data[name]
    return None


def _seed_owner(state_dir: Path) -> str | None:
    """Which player the captured login belongs to.

    Read from the captured credentials, which keep the real (unredacted)
    platform id. Without this, a second account logging in would inherit the
    first player's progression.
    """
    # Pinned on first use. Credentials get overwritten whenever a different
    # account logs in, so reading them live would hand the captured profile to
    # whoever happened to log in most recently.
    pin = state_dir / "seed_owner.txt"
    if pin.exists():
        try:
            recorded = pin.read_text("utf-8").strip()
            if recorded:
                return recorded
        except OSError:
            pass

    creds = state_dir.parent / "session" / "credentials.json"
    if not creds.exists():
        creds = state_dir / "session" / "credentials.json"
    if not creds.exists():
        return None
    try:
        data = json.loads(creds.read_text("utf-8"))
    except (ValueError, OSError):
        return None
    player = data.get("playerId")
    if not player:
        return None
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        pin.write_text(str(player), encoding="utf-8")
    except OSError:
        pass
    return str(player)


DAILY_STUB: dict[str, Any] = {
    "expiryTime": "0001-01-01T00:00:00",
    "leaderboardType": 0,
    "leaderboard": None,
    "clearanceRate": 0.0,
    "routeID": 0,
}


def stable_seed(*parts: Any) -> int:
    """Deterministic signed 32-bit seed from arbitrary identity parts.

    Must be stable across runs and machines: the same temple identity always
    produces the same seed, which is what makes an offline temple replayable
    and shareable.

    The range is signed on purpose -- a captured response carried
    ``"dungeonSeed": -951959203``, so the client expects a signed int32.
    """
    joined = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.sha256(joined).digest()
    return int.from_bytes(digest[:4], "big", signed=True)


class OfflineBackend:
    """Implements the reconstructed wiby.net game service."""

    def __init__(self, state_dir: Path, archive: "Library | None" = None):
        self.archive = archive
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        # One profile per player, so a self-hosted server can serve several.
        # The captured login seeds only the account it actually came from.
        self.profiles = ProfileStore(
            self.dir / "profiles",
            seed=archive.user_response if archive else None,
            seed_owner=_seed_owner(state_dir),
        )
        # Reports of temples that cannot be finished, and the rerolls that
        # route around them. Shared with the state dir so a server operator's
        # blacklist travels with their archive.
        self.blacklist = Blacklist(self.dir / "blacklist.json")
        self.layouts_dir = self.dir / "layouts"
        self.layouts_dir.mkdir(exist_ok=True)
        self.ghosts_dir = self.dir / "ghosts"
        self.ghosts_dir.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        # Temples we handed a seed for but have no layout yet. When the client
        # uploads what it built, the pair becomes a permanent local temple.
        self._pending_responses: dict[tuple[int, int], dict[str, Any]] = {}
        self._state_path = self.dir / "state.json"
        self.state = self._load_state()

    def _caller_id(self, req: dict[str, Any]) -> Any:
        """The in-game id of whoever is asking.

        Captured /GetDungeon responses carried the caller's own id -- 62842 for
        one account, 404886 for the other -- never a fixed value. This used to
        answer with the server-wide default, so on a machine serving more than
        one person every player was told they were the same user.
        """
        player_id = _first(req, "playerId", "playerID")
        if player_id:
            return self.profiles.for_player(player_id).user_id(player_id)
        return self.state["userID"]

    def _profile_for(self, req: dict[str, Any]):
        return self.profiles.for_player(_first(req, "playerId", "playerID"))

    # ------------------------------------------------------------- state

    def _load_state(self) -> dict[str, Any]:
        """Load saved state over the defaults, never instead of them.

        Returning the file as-is meant the defaults only ever applied when there
        was no file at all, so a state.json missing one key -- written by an
        older build, cut short by a crash, or edited by hand -- took the whole
        of /GetDungeon down with a KeyError, which reaches the player as a
        connection failure with nothing to explain it.
        """
        state = {
            "username": "Explorer",
            "userID": "offline-player-0001",
            "routeCounter": 1,
            "dungeons": {},
            "runs": [],
            "purchases": [],
            "statistics": {},
        }
        if self._state_path.exists():
            try:
                saved = json.loads(self._state_path.read_text("utf-8"))
                if isinstance(saved, dict):
                    state.update(saved)
            except (ValueError, OSError):
                pass
        return state

    def _save(self) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    # --------------------------------------------------------- endpoints

    def redirect(self, _req: dict[str, Any], base_url: str) -> dict[str, Any]:
        """`/redirectV3.json` -> InitialServerResponse.

        `targetEndpoint` is how the client learns where the real game service
        lives; pointing it back at us keeps everything on localhost.
        """
        captured = self.archive.captured("WakeUp") if self.archive else None
        if captured is not None:
            out = dict(captured)
            out["targetEndpoint"] = base_url
            out["maintenanceInfo"] = self._maintenance()
            return out

        return {
            "targetEndpoint": base_url,
            "maintenanceInfo": self._maintenance(),
            "globalValues": {
                "keyConversionUpRates": [0, 3, 3, 3, 3],
                "keyConversionDownRates": [0, 2, 2, 2, 2],
                "relicConversionRewards": {
                    "rewards": [],
                    "rewardsByArea": {},
                },
            },
        }

    def _maintenance(self) -> dict[str, Any]:
        """The `serverStatus` / `maintenanceInfo` object, captured verbatim.

        Note this is an **object**, not an integer. Real responses carry::

            "serverStatus": {"mode": 1, "newGamesLockedOut": false,
                             "maintenanceTimeUTC": "...", "serverVersion": 128,
                             "serverProtocolVersion": 7}
        """
        return {
            "mode": MAINTENANCE_MODE_OK,
            "newGamesLockedOut": False,
            "maintenanceTimeUTC": _utc().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "serverVersion": SERVER_VERSION,
            "serverProtocolVersion": SERVER_PROTOCOL_VERSION,
        }

    def wake_up(self, req: dict[str, Any], base_url: str) -> dict[str, Any]:
        """`/WakeUp` -> WakeUpResponse.

        Replays the captured response when we have one; its `globalValues`
        carry the real relic-conversion and key-exchange tables, which a
        synthesised stub cannot invent.
        """
        captured = self.archive.captured("WakeUp") if self.archive else None
        if captured is not None:
            out = dict(captured)
            out["targetEndpoint"] = base_url
            out["maintenanceInfo"] = self._maintenance()
            return out

        return self.redirect(req, base_url)

    def maintenance_status(self, _req: dict[str, Any]) -> dict[str, Any]:
        return self._maintenance()

    def verify_user(self, req: dict[str, Any]) -> dict[str, Any]:
        """`/VerifyUserID` -> WIBYUserResponse.

        The real request, captured from the client, looks like::

            {"userId": 0, "playerId": "<steamid64>", "platform": "STEAM",
             "verificationToken": "<steam auth ticket>",
             "platformVerificationId": "",
             "clientDungeonVersion": 128, "clientProtocolVersion": 7}

        Offline there is nothing to verify against, so any identity is accepted
        and the player's own platform id is adopted as their stable user id.
        """
        name = _first(req, "currentUsername", "requestedUserName") or \
            self.state["username"]
        player_id = req.get("playerId")
        with self._lock:
            self.state["username"] = name
            # Deliberately not recording the platform id. It is per-player data
            # in a server-wide slot, so on a shared server it held whoever
            # signed in last -- and it was the only reason a SteamID64 appeared
            # in state.json at all.
            self.state.pop("platformID", None)
            self._save()

        profile = self.profiles.for_player(player_id, name=name)
        # The client sends no name when it uploads a run, so this is what a
        # phantom of theirs will be labeled with.
        profile.set_name(name)

        # Replay this player's own captured login if there is one: whips,
        # upgrades, relics and statistics all live there, and handing them a
        # blank profile would look like a wiped save.
        #
        # A capture belongs to the account named in it and to nobody else. On a
        # server other people connect to, replaying it to all comers would give
        # a stranger the host's save -- and, because /SubmitRun takes the name
        # from the profile, would then file that stranger's runs under the
        # host's name.
        # ...and to one save. A player can hold several profiles, all signing
        # in under the same display name, so matching on the name alone handed
        # every one of them the captured login -- and with it every field a
        # blank profile does not override: relics, locked routes, the last
        # route played. A second profile arrived already finished.
        #
        # The capture belongs to the profile that inherited it, which is the
        # one recorded as the seed owner. Every other profile is a player this
        # archive has not met.
        owns_capture = (
            self.profiles.seed_owner is not None
            and ProfileStore._safe(player_id) == self.profiles.seed_owner
        )
        captured = (
            self.archive.login_for_name(name)
            if self.archive and owns_capture else None
        )
        # Someone we have never met gets what the live service sent a new
        # account: a real first-connection response, with our own player in it.
        template = captured or (self.archive.new_player_login()
                                if self.archive else None)
        if template is not None:
            out = dict(template)
            # The profile is authoritative for anything the player can change;
            # the template only supplies the fields we never track.
            out.update(profile.snapshot())
            out["currentUsername"] = name
            out["userID"] = profile.user_id(player_id)
            out["userId"] = out["userID"]
            out["playerId"] = player_id or ""
            out["targetEndpoint"] = None
            # Captured logins carry the identity of whoever was captured, and
            # a redacted placeholder where the archive removed it. Neither
            # belongs in somebody else's login.
            out["sharerID"] = ""
            out["platformVerificationID"] = req.get("platformVerificationId") or ""
            out["verificationToken"] = "offline"
            # The captured daily dungeon expired long ago.
            out["dailyDungeonInfo"] = self._daily_info(0)
            out["dailyDungeonInfoYesterday"] = self._daily_info(-1)
            if captured is None:
                # Nothing of the captured account's own progress survives.
                out["lastRouteInfo"] = self._route_info()
            out["serverStatus"] = self._maintenance()
            return out

        daily = self._daily_info(0)
        return {
            "serverStatus": self._maintenance(),
            "serverVersion": SERVER_VERSION,
            "serverProtocolVersion": SERVER_PROTOCOL_VERSION,
            "currentUsername": name,
            "userID": self.state["userID"],
            "userId": self.state["userID"],
            # Echo the caller. This used to read a server-wide field that no
            # longer exists -- it held whoever signed in last, which was wrong
            # on a shared server and was removed -- so every client was told
            # its own id was empty.
            "playerId": player_id or "",
            "platform": req.get("platform", "STEAM"),
            "platformVerificationID": self.state["userID"],
            "verificationToken": "offline",
            "lastRouteInfo": self._route_info(),
            "victoryRoutes": [],
            "lockedRoutes": [],
            "permanentPurchases": self.state["purchases"],
            "dailyDungeonInfo": daily,
            "dailyDungeonInfoYesterday": self._daily_info(-1),
            "playerStatistics": self.state["statistics"],
        }

    def refresh_verification(self, req: dict[str, Any]) -> dict[str, Any]:
        return self.verify_user(req)

    def _route_info(self) -> dict[str, Any]:
        return {
            "completedByID": "",
            "completedByName": "",
            "routeAttemptCount": 0,
            "sandbag": False,
            "wageredWhipID": -1,
            "storedCurrency": 0,
        }

    # ----------------------------------------------------------- dungeon

    def get_dungeon(self, req: dict[str, Any], base_url: str) -> dict[str, Any]:
        """`/GetDungeon` -> WIBYDungeonResponse. The seed lives here.

        The real request, captured from the client::

            {"gameMode": "DGM_ADVENTURE", "dungeonSettingsIndex": 0,
             "routeId": 0, "routeStage": 0, "dungeonFloorNumber": 0,
             "shareCode": "", "knownDungeons": [], "whipWagered": "Bamboo",
             "dungeonId": 0, "isRetry": false, "difficultyRating": 0,
             "curseLevel": 530, "platformFriends": [...]}

        Note the casing: `routeId`/`dungeonId`, not `requestedRouteID`.
        """
        route_id = int(_first(req, "routeId", "routeID", "requestedRouteID") or 0)
        if route_id <= 0:
            with self._lock:
                route_id = int(self.state.get("routeCounter") or 1)
                self.state["routeCounter"] = route_id + 1
                self._save()

        floor = int(_first(req, "dungeonFloorNumber", "floorIndex") or 0)
        stage = int(_first(req, "routeStage", "stageIndex") or 0)
        dungeon_id = int(_first(req, "dungeonId", "dungeonID") or 0)
        # A request for dungeon 0 is "mint me a new temple". The real server
        # answers those with dungeonFloorTotalCount 0 -- it has not decided yet.
        wants_new_temple = dungeon_id <= 0
        settings_index = int(_first(req, "dungeonSettingsIndex") or 0)
        game_mode = req.get("gameMode") or "DGM_ADVENTURE"

        # Abyss routes descend through four areas, one dungeon each. The client
        # signals "next area" by asking for dungeonId 0 on a route it already
        # holds, so the server has to remember how far down the route is.
        #
        # This must happen before the seed key is built: otherwise every area
        # hashes from dungeonId 0 and all four first floors share a seed, so
        # the client builds the same temple in Ruins, Caverns, Inferno and Rift.
        abyss = game_mode == "DGM_CLASSIC"
        if abyss:
            stage, route_id, dungeon_id = self._abyss_position(
                req, route_id, dungeon_id, floor
            )
            # In Abyss the settings index IS the stage: 0/1/2/3 for
            # Ruins/Caverns/Inferno/Rift. Confirmed across 380 dungeons in
            # other players' shared routes.
            #
            # This picks the dungeon settings collection, which carries
            # TempleKeyChances and GetGuardianStartingLevel among other things.
            # Sending 0 for every area generated a Rift floor with Ruins key
            # settings -- the temple built fine but the exit door wanted a key
            # that was never placed, so the run was unwinnable.
            settings_index = stage

        # Distinct temples must not collide, so identity includes mode and
        # settings, not just the route/floor coordinates.
        key = f"{game_mode}:{route_id}:{stage}:{floor}:{dungeon_id}:{settings_index}"

        # Abyss floor counts come from the difficulty tier; Adventure is 3.
        # These decide the temple's shape, so they are needed before the seed
        # can be checked against the blacklist.
        difficulty = int(req.get("difficultyRating") or 0)
        if abyss:
            total_floors = abyss_floors(difficulty, stage)
        else:
            total_floors = 3
        shape = floor_shape(floor, total=total_floors, abyss=abyss)
        area_id = (stage + 1) if abyss else int(req.get("areaID") or 1)

        with self._lock:
            record = self.state["dungeons"].get(key)
            if record is None:
                record = {
                    "routeID": route_id,
                    "routeStage": stage,
                    "floor": floor,
                    "dungeonSeed": stable_seed(key),
                    "attempts": 0,
                }
                self.state["dungeons"][key] = record
            record["attempts"] += 1
            attempts = record["attempts"]
            total_floors = int(record.get("dungeonFloorTotalCount") or total_floors)

            # A temple somebody reported as unfinishable must not be handed out
            # again. There is nothing to repair -- the layout is whatever the
            # seed builds -- so the cure is a different seed in the same slot.
            #
            # The chosen reroll is persisted: a rerolled temple has to stay put
            # across restarts, or the player's temple changes under them.
            if self.blacklist is not None:
                template = {
                    "gameMode": GAME_MODES.get(game_mode, 1),
                    "areaID": area_id,
                    "dungeonSettingsIndex": settings_index,
                    # Must describe exactly what a generated floor is served
                    # as, or a report and the reroll that answers it would
                    # fingerprint two different temples and never match.
                    "dungeonLayoutType": 0,
                    "dungeonFloorType": 0,
                    "difficultyRating": difficulty,
                }
                seed, reroll = unblocked_seed(
                    self.blacklist,
                    lambda n: stable_seed(key) if n == 0 else stable_seed(key, n),
                    template,
                    start=int(record.get("reroll") or 0),
                )
                if seed != record["dungeonSeed"]:
                    print(
                        f"[blacklist] {key} was reported unplayable; "
                        f"rerolled to seed {seed}"
                    )
                    record["dungeonSeed"] = seed
                    record["reroll"] = reroll
                    # The old layout describes the broken temple. Drop it so
                    # the client builds the replacement rather than replaying
                    # the very temple that was reported.
                    record.pop("dungeonLayoutType", None)
                    record.pop("dungeonFloorType", None)
            self._save()

        # The whip and the challenge level are stated when the route is asked
        # for and never again: /SubmitRun carries neither, even on a run that
        # wagered a whip at curseLevel 1000. The real server plainly remembers
        # them per route -- its victory routes carry curseLevel 530 for Bamboo
        # and 670 for Scales, matching that account's challenge levels exactly.
        #
        # Reading curseLevel off the submission instead recorded 0 for every
        # route, so a challenge beaten offline was banked as no challenge at
        # all and the player's level never moved.
        whip = req.get("whipWagered")
        curse = req.get("curseLevel")
        if whip or curse:
            with self._lock:
                if whip:
                    whips = dict(self.state.get("routeWhips") or {})
                    whips[str(route_id)] = whip
                    self.state["routeWhips"] = whips
                if curse:
                    curses = dict(self.state.get("routeCurses") or {})
                    curses[str(route_id)] = int(curse)
                    self.state["routeCurses"] = curses
                self._save()

        archived = self.archive.lookup(dungeon_id, floor) if self.archive else None

        # A fresh route asks for dungeon 0 and expects the server to assign one.
        # Substitute an archived temple so the player gets a real one.
        # A pinned temple overrides selection entirely, so every player on this
        # server gets the same one. Useful for a shared challenge or for testing.
        pinned = self._pinned_temple(floor)
        if pinned is not None and dungeon_id <= 0:
            archived = pinned

        # An archived temple that was reported unplayable cannot be rerolled --
        # the layout is fixed bytes from the CDN, not something a seed rebuilds.
        # The only remedy is to serve a different one. This is the case that
        # covers the developer-acknowledged impossible temples, which survive in
        # the archive exactly because nobody could ever finish them.
        if archived is not None and self._temple_blocked(archived):
            print(
                f"[blacklist] archived dungeon {archived.dungeon_id} "
                f"floor {floor} was reported unplayable; skipping it"
            )
            archived = None

        substituted = False
        # The daily is chosen by the date, not by the rotation. One temple, the
        # same one for everybody, until midnight UTC -- take that away and it
        # is an ordinary temple with a timer on it. The rotation counter was
        # handing each request the next temple in the list, so two players on
        # the same day got different dailies.
        if (archived is None and self.archive is not None and dungeon_id <= 0
                and GAME_MODES.get(game_mode) == GAME_MODES["DGM_DAILY"]):
            from . import daily as daily_temples

            wanted = daily_temples.choose(self.dir, self.archive)
            if wanted is not None:
                candidate = self.archive.lookup(wanted, floor)
                if candidate is not None and not self._temple_blocked(candidate):
                    archived = candidate
                    substituted = True

        if archived is None and self.archive is not None and dungeon_id <= 0:
            with self._lock:
                pick = int(self.state.get("templeRotation", 0))
                if floor == 0:
                    # Advance only when a new run starts, so every floor of the
                    # same run comes from the same temple.
                    self.state["templeRotation"] = pick + 1
                    self._save()
            # Walk past blacklisted temples rather than giving up on the
            # archive entirely -- one bad temple must not cost the player every
            # real one behind it in the rotation.
            beaten = self.profiles.for_player(
                _first(req, "playerId", "playerID")
            ).completed_dungeons()
            for offset in range(MAX_ROTATION_SKIPS):
                candidate = self.archive.any_for_floor(
                    floor,
                    pick=pick + offset,
                    game_mode=GAME_MODES.get(game_mode),
                    difficulty=difficulty,
                    exclude=beaten,
                )
                if candidate is None or not self._temple_blocked(candidate):
                    archived = candidate
                    break
            substituted = archived is not None

        # Prefer replaying the genuine response for a temple we captured: its
        # seed and floor/layout types are known-good and match the archived
        # layout blob exactly.
        if archived is not None:
            replayed = archived.replay(base_url)
            if replayed is not None:
                replayed["serverStatus"] = self._maintenance()
                replayed["totalDungeonAttemptsAllFloors"] = attempts
                # The temple's geometry is what the archive is for. Its route
                # identity is not: a captured response still names the route it
                # was served on, belonging to whoever originally ran it. The
                # client reads routeID back to us on the next request, so
                # replaying it verbatim hands our bookkeeping a stranger's
                # route -- which stalled the Abyss stage and revisited an area.
                replayed["routeID"] = route_id
                replayed["routeStage"] = stage
                replayed["userID"] = self._caller_id(req)
                if abyss:
                    replayed["areaID"] = area_id
                    replayed["dungeonSettingsIndex"] = settings_index
                    replayed["difficultyRating"] = difficulty
                self._remember_dungeon(replayed)
                self._remember_served(_first(req, "playerId", "playerID"), replayed)
                if substituted:
                    print(
                        f"[offline] no archived temple for dungeon {dungeon_id} "
                        f"floor {floor}; serving archived dungeon "
                        f"{archived.dungeon_id} instead"
                    )
                return replayed

        # No archived layout: hand over a seed with an empty layout URL and let
        # the client build the temple. It uploads the result to
        # /SubmitDungeonLayout, which is what makes new offline content
        # possible once the CDN is gone.
        if dungeon_id <= 0:
            dungeon_id = abs(record["dungeonSeed"]) % 900000 + 100000
            self._note_minted(dungeon_id)

        # When the server has no layout to hand over, it describes no shape
        # either: the client is generating the floor and decides for itself.
        #
        # Measured across 2,207 captured responses with no exceptions:
        #
        #   layout sent  -> real dungeonLayoutType / dungeonFloorType
        #   no layout    -> both 0
        #   floor count  -> 0 for a temple that does not exist yet,
        #                   the known count once it does
        #
        # Sending an invented shape here overrides the client's own logic for a
        # floor it is about to build. That is how an Abyss floor ended up with
        # an exit door whose key was never placed.
        # Reported on the wire only. `total_floors` stays intact for our own
        # bookkeeping -- the real server plainly knows the count too, it just
        # does not state it until the temple exists.
        shape = (0, 0)
        reported_total = 0 if wants_new_temple else total_floors

        # Shape mirrors a captured 200 response from the real server.
        synthesized = {
            "gameMode": GAME_MODES.get(game_mode, 1),
            "serverStatus": self._maintenance(),
            "userID": self._caller_id(req),
            "routeID": route_id,
            "playerCount": 0,
            "deathCount": 0,
            "routeStage": stage,
            "dungeonID": dungeon_id,
            "dungeonSeed": record["dungeonSeed"],
            "areaID": area_id,
            "difficultyRating": difficulty,
            "dungeonSettingsIndex": settings_index,
            "dungeonFloorNumber": floor,
            "dungeonLayoutType": record.get("dungeonLayoutType", shape[0]),
            "dungeonFloorTotalCount": reported_total,
            "dungeonFloorType": record.get("dungeonFloorType", shape[1]),
            "dungeonVersion": SERVER_VERSION,
            "totalDungeonAttemptsAllFloors": attempts,
            "temporaryPowerIndex": -1,
            "whipWagered": None,
            "relicIDs": None,
            "relicCollectorIDs": None,
            "relicCollectorNames": None,
            "health": {"baseHealth": 3, "baseDailyHealth": 3},
            "precalculatedRooms": None,
            "layoutDownloadURL": archived.layout_url(base_url) if archived else "",
            "persistentChanges": None,
            "ghostRuns": archived.ghost_runs(base_url) if archived else [],
        }
        synthesized["dungeonID"] = dungeon_id

        # Remember it so the client's upload can be paired with this response.
        self._pending_responses[(dungeon_id, floor)] = dict(synthesized)
        self._remember_dungeon(dict(synthesized, dungeonFloorTotalCount=total_floors))
        self._remember_served(_first(req, "playerId", "playerID"), synthesized)
        return synthesized

    def _temple_blocked(self, temple: Any) -> bool:
        """Whether a stored temple has been reported unplayable.

        Both report kinds have to be checked here. A temple the client
        generated and uploaded is stored like any other, and is looked up by
        dungeon id on the way back in -- so without the fingerprint check a
        generated temple would be replayed straight from storage and never
        reach the reroll, which is precisely the case the reroll exists for.
        """
        if self.blacklist is None or temple is None:
            return False
        if self.blacklist.blocks_temple(temple.dungeon_id, temple.floor):
            return True
        response = getattr(temple, "response", None)
        if isinstance(response, dict):
            return self.blacklist.blocks_fingerprint(
                Fingerprint.from_response(response)
            )
        return False

    def _remember_served(self, player_id: Any, response: dict[str, Any]) -> None:
        """Record the last temple handed to a player, so it can be reported.

        A player who hits an unfinishable temple is inside the game, with no
        dungeon id in front of them and no way to read one. Keeping the last
        response per player means the report can be made afterwards from the
        command line without asking them to copy anything down.
        """
        if not player_id:
            return
        keep = (
            "dungeonID", "dungeonFloorNumber", "dungeonSeed", "areaID",
            "gameMode", "dungeonSettingsIndex", "dungeonLayoutType",
            "dungeonFloorType", "dungeonFloorTotalCount", "difficultyRating",
            "routeID", "routeStage",
        )
        entry = {k: response.get(k) for k in keep}
        # Whether the temple came from the archive or was generated decides
        # which kind of report it needs, and it cannot be inferred later.
        entry["archived"] = bool(response.get("layoutDownloadURL"))
        key = self._player_key(player_id)
        with self._lock:
            served = {
                k: v
                for k, v in (self.state.get("lastServed") or {}).items()
                # Drop anything keyed by a platform id. This map used to be
                # keyed by SteamID64, which made a server's own bookkeeping a
                # record of everyone who had ever connected to it.
                if not _LOOKS_LIKE_A_STEAM_ID.match(str(k))
            }
            served[key] = entry
            self.state["lastServed"] = served

            # A short history as well, because a player reports the floor that
            # stopped them, which by then may be one or two floors back. It is
            # also what makes a guest's report checkable: this server can say
            # whether they were ever handed the temple they are condemning.
            history = {
                k: v
                for k, v in (self.state.get("servedHistory") or {}).items()
                if not _LOOKS_LIKE_A_STEAM_ID.match(str(k))
            }
            mine = [e for e in (history.get(key) or [])
                    if not _same_temple(e, entry)]
            mine.append(entry)
            history[key] = mine[-SERVED_HISTORY:]
            self.state["servedHistory"] = history
            self._save()

    def _player_key(self, player_id: Any) -> str:
        """How a player is referred to in the server's own bookkeeping.

        Their in-game id, which is allocated by us and means nothing outside
        this archive -- not the platform id they signed in with.
        """
        if not player_id:
            return "anyone"
        return str(self.profiles.for_player(player_id).user_id(player_id))

    def last_served(self, player_id: Any = None) -> dict[str, Any] | None:
        """The most recent temple served, for a player or for anyone."""
        served = self.state.get("lastServed") or {}
        if player_id:
            return served.get(self._player_key(player_id))
        if not served:
            return None
        return list(served.values())[-1]

    def _remember_dungeon(self, response: dict[str, Any]) -> None:
        """Keep a temple's floor count and area for later route records."""
        dungeon_id = response.get("dungeonID")
        if not dungeon_id:
            return
        with self._lock:
            floors = dict(self.state.get("dungeonFloors") or {})
            areas = dict(self.state.get("dungeonAreas") or {})
            floors[str(dungeon_id)] = response.get("dungeonFloorTotalCount") or 0
            areas[str(dungeon_id)] = response.get("areaID") or 0
            self.state["dungeonFloors"] = floors
            self.state["dungeonAreas"] = areas
            self._save()

    def submit_layout(self, req: dict[str, Any]) -> dict[str, Any]:
        """`/SubmitDungeonLayout` -> DungeonSubmissionResponse.

        This is how an offline server keeps making new content. When we hand
        the client a seed with no ``layoutDownloadURL``, it builds the temple
        itself (``UDungeonBuilder``) and posts the result here. Storing it under
        the same naming the CDN used means the archive indexes it exactly like
        a downloaded one, and the temple is replayable from then on.

        Never observed in captured traffic, because every temple the live
        server served already had a layout.
        """
        dungeon_id = int(_first(req, "dungeonId", "dungeonID") or 0)
        floor = int(_first(req, "dungeonFloorNumber", "floorIndex") or 0)
        layout_data = _first(req, "savedLayoutData", "layoutData")

        stored_name: str | None = None
        if (
            self.archive is not None
            and dungeon_id > 0
            and isinstance(layout_data, str)
            and layout_data
        ):
            stored_name = (
                f"local__dungeon-{dungeon_id}-floor-{floor}-layout-generated"
            )
            try:
                self.archive.dir.mkdir(parents=True, exist_ok=True)
                (self.archive.dir / stored_name).write_text(
                    layout_data, encoding="utf-8"
                )
            except OSError as exc:
                print(f"[offline] could not store generated layout: {exc}")
                stored_name = None

        # Keep the raw submission too; it carries fields the layout blob does
        # not (area, floor type, permanent settings).
        record = {
            "dungeonID": dungeon_id,
            "dungeonFloorNumber": floor,
            "routeID": _first(req, "routeId", "routeID"),
            "routeStage": req.get("routeStage"),
            "dungeonFloorCount": _first(req, "dungeonFloorCount", "dungeonFloorTotalCount"),
            "dungeonLayoutType": req.get("dungeonLayoutType"),
            "dungeonFloorType": req.get("dungeonFloorType"),
            "dungeonVersion": req.get("dungeonVersion"),
            "dungeonSettingsIndex": req.get("dungeonSettingsIndex"),
            "areaID": req.get("areaID"),
            "permanentSettingsData": req.get("permanentSettingsData"),
            "storedLayout": stored_name,
        }
        try:
            self.layouts_dir.mkdir(parents=True, exist_ok=True)
            (self.layouts_dir / f"{dungeon_id}_{floor}.json").write_text(
                json.dumps(record, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            print(f"[offline] could not record layout submission: {exc}")

        if self.archive is not None and dungeon_id > 0:
            pending = self._pending_responses.pop((dungeon_id, floor), None)
            if pending is not None or stored_name:
                response = dict(pending or {})
                if stored_name:
                    # The response we sent said nothing about the temple's
                    # shape, because at that point no layout existed and the
                    # real server states none in that case (1m). Now one does,
                    # and replaying those zeroes alongside a layout hands the
                    # client geometry it has no description for -- observed
                    # live as a crash on level load.
                    #
                    # The submission itself says what was built, so take the
                    # shape from there. It is the client's own account of the
                    # temple it just generated, which is as authoritative as
                    # this gets.
                    shape = {
                        "dungeonLayoutType": req.get("dungeonLayoutType"),
                        "dungeonFloorType": req.get("dungeonFloorType"),
                        "dungeonFloorTotalCount": _first(
                            req, "dungeonFloorCount", "dungeonFloorTotalCount"
                        ),
                        "dungeonSettingsIndex": req.get("dungeonSettingsIndex"),
                    }
                    for key, value in shape.items():
                        if isinstance(value, int) and value > 0:
                            response[key] = value
                self.archive.record_local_temple(
                    dungeon_id, floor, response, stored_name,
                    origin=self._origin_of(dungeon_id),
                )

        return {
            "serverStatus": self._maintenance(),
            "accepted": True,
            "dungeonID": dungeon_id,
            "dungeonFloorNumber": floor,
        }


    # -------------------------------------------------------------- runs

    def report_temple(self, req: dict[str, Any]) -> dict[str, Any]:
        """Take a report of an unfinishable temple from whoever is playing.

        Not a game endpoint -- the client has no report button and never calls
        this. It exists so a player's own copy of this program can report to a
        server it does not own, which is the only way a shared server ever
        learns that one of its temples cannot be finished.

        Everything here arrives from a stranger, so two things are checked:

        * **They played it.** A report for a temple this server never handed
          them is refused. Without that, one request in a loop condemns the
          whole archive, and the reporter never has to load the game.
        * **They are one voice.** The report is recorded as a guest's, so it
          withholds nothing on its own -- it waits for somebody else who ran
          the same temple to agree. Pressing the button twice changes nothing.
        """
        if self.blacklist is None:
            return {"accepted": False, "reason": "no blacklist on this server"}

        player_id = _first(req, "playerId", "playerID")
        dungeon = int(_first(req, "dungeonId", "dungeonID") or 0)
        floor = int(_first(req, "dungeonFloorNumber", "floor") or 0)
        reason = str(req.get("reason") or "other")
        if reason not in REASONS:
            reason = "other"

        played = self._served_entry(player_id, dungeon, floor)
        if played is None:
            return {
                "accepted": False,
                "reason": "this server has not served you that temple",
            }

        if played.get("archived"):
            report = self.blacklist.report_archived(
                dungeon, floor, reason=reason, note=str(req.get("note") or "")[:200],
                player_id=player_id, source=SOURCE_GUEST,
            )
        else:
            report = self.blacklist.report_generated(
                Fingerprint.from_response(played), reason=reason,
                note=str(req.get("note") or "")[:200],
                player_id=player_id, source=SOURCE_GUEST,
            )
        return {
            "accepted": True,
            "dungeonID": dungeon,
            "dungeonFloorNumber": floor,
            "reports": report.count,
            "withheld": report.blocks(),
            "needed": max(0, IMPORT_THRESHOLD - report.count),
        }

    def _served_entry(
        self, player_id: Any, dungeon: int, floor: int
    ) -> dict[str, Any] | None:
        """The temple this player was handed, if this server handed it to them."""
        key = self._player_key(player_id)
        history = (self.state.get("servedHistory") or {}).get(key) or []
        latest = (self.state.get("lastServed") or {}).get(key)
        for entry in list(history) + ([latest] if latest else []):
            if not isinstance(entry, dict):
                continue
            if (int(entry.get("dungeonID") or 0) == dungeon
                    and int(entry.get("dungeonFloorNumber") or 0) == floor):
                return entry
        return None

    def complete_tutorial(self, req: dict[str, Any]) -> dict[str, Any]:
        """`/CompleteTutorialDungeon` -> WIBYRunUploadReceipt.

        Answered with a bare acknowledgment until now, which was fine while
        every account had already done the tutorial years ago. Profiles made
        new accounts real, and a new account starts here.

        The captured request and reply say what it is: a run submission and a
        run receipt. It awards the first dungeon key, collects the Tutorial
        relic, and -- through `apply_run` -- is what marks the account as no
        longer new. Returning only `serverStatus` meant none of that happened,
        so the client had no acknowledgment to act on: it ran the tutorial
        again, and then sat waiting for a server it was already talking to.

        Verified against the one captured completion: keys went [1,0,0,0] ->
        [2,0,0,0] and the reply carried the player's own id and totals.
        """
        profile = self._profile_for(req)
        banked = profile.apply_run(req)

        with self._lock:
            attempt_id = len(self.state["runs"]) + 1
            self.state["runs"].append({
                "attemptID": attempt_id,
                "at": _ue_time(_utc()),
                "dungeonID": 0,
                "dungeonFloorNumber": _first(req, "dungeonFloorNumber") or 0,
                "lifetime": req.get("lifetime"),
                "success": req.get("success"),
                "tutorial": True,
            })
            self._save()

        snapshot = profile.snapshot()
        return {
            "userID": self._caller_id(req),
            "dungeonID": 0,
            "routeID": _first(req, "routeId", "routeID") or 0,
            "dungeonFloorNumber": _first(req, "dungeonFloorNumber") or 0,
            "success": bool(req.get("success")),
            "isSandbag": bool(req.get("isSandbag")),
            "routeTotalEssence": int((req.get("currency") or {}).get("essence") or 0),
            "updatedReputation": int(snapshot.get("reputation") or 0),
            "temporaryPowerIndex": -1,
            "health": None,
            "currency": banked if banked is not None else snapshot.get("currency"),
            "lastRunRouteInfo": None,
            "activeClassicRoutes": snapshot.get("activeClassicRoutes") or [],
            "maintenanceInfo": self._maintenance(),
            "attemptID": attempt_id,
            "dailySubmissionResponse": self._daily_block(),
        }

    def submit_run(self, req: dict[str, Any]) -> dict[str, Any]:
        """`/SubmitRun` -> WIBYRunUploadReceipt.

        The client posts its phantom recording inline as `runData` (a few
        hundred KB). Offline we keep it and register it as a phantom for that
        temple, so the player's own ghost is there on the next visit -- which
        is what the real service does, and the only way an offline temple stays
        populated once other players' phantoms stop arriving.
        """
        dungeon_id = int(_first(req, "dungeonId", "dungeonID") or 0)
        floor = int(_first(req, "dungeonFloorNumber", "floorIndex") or 0)
        run_data = req.get("runData")

        with self._lock:
            attempt_id = len(self.state["runs"]) + 1
            record = {
                "attemptID": attempt_id,
                "at": _ue_time(_utc()),
                "dungeonID": dungeon_id,
                "dungeonFloorNumber": floor,
                "lifetime": req.get("lifetime"),
                "success": req.get("success"),
                "endLocation": req.get("endLocation"),
                "gameMode": req.get("gameMode"),
            }
            self.state["runs"].append(record)
            self._save()

        # Bank the run. Returns the new totals at route end, None mid-route --
        # matching the live server, which sends `currency: null` between floors.
        profile = self.profiles.for_player(_first(req, "playerId", "playerID"))
        banked = profile.apply_run(req)
        # Route history is where relics, whips and challenge completion actually
        # live -- the client derives all three from it.
        #
        # Only a completed temple (success 2) earns a place. Confirmed against
        # captured logins: a route with a completion went 55 -> 56 victoryRoutes,
        # while a route that ended in death with no completion stayed at 56.
        #
        # The live server appends when the whole route ends; we append at each
        # completion instead. The end state is identical, and recording sooner
        # means a relic is not lost if the session stops mid-route.
        if req.get("success") == RUN_TEMPLE_COMPLETED:
            profile.record_victory(self._as_history(self._route_info_for(req)))
        stored = self._store_run(req, dungeon_id, floor, attempt_id, run_data)

        # Shape mirrors a captured 200 response exactly: no serverStatus here
        # (only /GetDungeon carries it), `success` and `isSandbag` are bools,
        # and `currency` is null mid-route.
        return {
            "userID": _first(req, "userId", "userID") or 0,
            "dungeonID": dungeon_id,
            "routeID": _first(req, "routeId", "routeID") or 0,
            "dungeonFloorNumber": floor,
            "success": bool(req.get("success")),
            "isSandbag": bool(req.get("isSandbag")),
            "routeTotalEssence": int((req.get("currency") or {}).get("essence") or 0),
            "updatedReputation": int(
                self.profiles.for_player(
                    _first(req, "playerId", "playerID")
                ).snapshot().get("reputation")
                or 0
            ),
            "temporaryPowerIndex": -1,
            "health": None,
            "currency": banked,
            "lastRunRouteInfo": self._route_info_for(req) if banked else None,
            "attemptID": attempt_id,
            "activeClassicRoutes": [],
            "dailySubmissionResponse": self._daily_block(),
            "maintenanceInfo": self._maintenance(),
        }

    def _abyss_position(
        self, req: dict[str, Any], route_id: int, dungeon_id: int, floor: int
    ) -> tuple[int, int, int]:
        """Track how far down an Abyss route the player is.

        Returns (stage, routeId, dungeonId). A request for dungeonId 0 on an
        existing route advances to the next area and mints a dungeon for it;
        a request naming a dungeon simply continues in that one.

        The live server refuses this without a submitted run in between, but we
        are the server, so an offline player can descend all four areas without
        anything being fabricated.
        """
        with self._lock:
            routes = dict(self.state.get("abyssRoutes") or {})

            if route_id <= 0:
                route_id = int(self.state.get("routeCounter") or 1)
                self.state["routeCounter"] = route_id + 1

            entry = dict(routes.get(str(route_id)) or {})
            if dungeon_id > 0:
                # Continuing in a known dungeon; keep the stage we recorded.
                stage = int(entry.get("stage", 0))
            elif entry:
                stage = min(int(entry.get("stage", 0)) + 1, ABYSS_AREAS - 1)
            else:
                stage = 0

            if dungeon_id <= 0:
                # Prefer a real archived temple for this area over a generated
                # one. Phantoms are the scarce thing -- a generated temple is
                # always available afterwards, but a recording of someone's run
                # cannot be made again.
                #
                # Skip temples this player has already beaten: they have seen
                # those phantoms, so re-serving one spends the archive on
                # somebody with nothing left to find in it.
                adopted = None
                if self.archive is not None:
                    beaten = self.profiles.for_player(
                        _first(req, "playerId", "playerID")
                    ).completed_dungeons()
                    adopted = self.archive.abyss_temple(
                        stage + 1,
                        abyss_floors(int(req.get("difficultyRating") or 0), stage),
                        pick=int(self.state.get("templeRotation") or 0),
                        exclude=beaten,
                    )
                if adopted is not None:
                    dungeon_id = adopted
                    self.state["templeRotation"] = (
                        int(self.state.get("templeRotation") or 0) + 1
                    )
                else:
                    dungeon_id = abs(
                        stable_seed("abyss", route_id, stage)
                    ) % 900000 + 100000
                    self._note_minted(dungeon_id, locked=True)

            entry.update({"stage": stage, "dungeon": dungeon_id})
            routes[str(route_id)] = entry
            self.state["abyssRoutes"] = routes
            self._save()

        return stage, route_id, dungeon_id

    def sync_from_capture(self) -> list[str]:
        """Carry progression from the last live session into offline play.

        A profile is seeded once and then owned by this server, so a player who
        went online afterwards would find their offline account frozen at
        whenever it was first created. This was a command somebody had to
        remember to run; now it happens when the offline server starts.

        Only newer captures are applied. The timestamp of the one already
        applied is kept on the profile, so restarting the server repeatedly
        does not keep overwriting offline progress with the same stale login.
        """
        if self.archive is None:
            return []

        from .profile import PROFILE_FIELDS

        updated: list[str] = []
        for user_id, login in (self.archive.user_responses or {}).items():
            player = login.get("platformVerificationID") or login.get("playerId")
            fresher = self.archive.latest_progression(user_id)
            stamp = max((fresher.get("_seenAt") or {}).values(), default="")

            # A profile is keyed by SteamID; a login is keyed by the numeric
            # userID. Nothing links the two until we do it here, so a profile
            # that has never been matched carries no userID at all and would be
            # skipped forever. Adopt it once, then the link is explicit.
            owner = self.profiles.seed_owner
            for known in self.profiles.known_players():
                profile = self.profiles.for_player(known)
                recorded = profile.data.get("userID")
                if recorded:
                    if int(recorded) != int(user_id):
                        continue
                elif owner and known != owner:
                    continue
                elif not owner and len(self.profiles.known_players()) > 1:
                    # Several accounts and nothing saying which owns this
                    # login: guessing would hand one player another's history.
                    continue
                profile.data["userID"] = int(user_id)
                if stamp and profile.data.get("syncedFromCapture", "") >= stamp:
                    continue  # already carried this session across

                for field in PROFILE_FIELDS:
                    value = login.get(field)
                    if value is not None:
                        profile.data[field] = value
                # A login is stale the moment the next run ends, so anything
                # the service said later wins over it.
                for field in ("currency", "reputation"):
                    value = fresher.get(field)
                    if value is not None:
                        profile.data[field] = value
                if stamp:
                    profile.data["syncedFromCapture"] = stamp
                profile.save()
                updated.append(known)
        return updated

    def observe_capture(
        self, path: str, request: dict[str, Any] | None, response: dict[str, Any] | None
    ) -> None:
        """Bank what a live session produced, as it happens.

        Forwarding preserves the traffic, and the archiver downloads whatever
        the CDN referenced. Neither covers content that only travels upward:
        a temple the client built because the service had none, and the run the
        player just recorded. Those exist solely in the request body, and if
        they are not taken here they are not taken at all.

        The ids come from the live service, so temples banked here are real and
        may be contributed to a pool -- unlike anything this server minted.
        """
        if self.archive is None:
            return
        endpoint = path.rsplit("/", 1)[-1].lower()

        if endpoint == "getdungeon" and isinstance(response, dict):
            dungeon_id = int(response.get("dungeonID") or 0)
            floor = int(response.get("dungeonFloorNumber") or 0)
            if dungeon_id > 0:
                # Held so an upload arriving later can be paired with the
                # response that described the temple.
                self._pending_responses[(dungeon_id, floor)] = dict(response)
            return

        if not isinstance(request, dict):
            return

        if endpoint == "submitdungeonlayout":
            self._bank_captured_layout(request)
        elif endpoint == "submitrun":
            self._bank_captured_run(request)

    def _bank_captured_layout(self, req: dict[str, Any]) -> None:
        """Store a temple the client built during a live session."""
        dungeon_id = int(_first(req, "dungeonId", "dungeonID") or 0)
        floor = int(_first(req, "dungeonFloorNumber", "floorIndex") or 0)
        layout = _first(req, "savedLayoutData", "layoutData")
        if dungeon_id <= 0 or not isinstance(layout, str) or not layout:
            return
        if self.archive.lookup(dungeon_id, floor) is not None:
            return

        name = f"local__dungeon-{dungeon_id}-floor-{floor}-layout-captured"
        try:
            self.archive.dir.mkdir(parents=True, exist_ok=True)
            (self.archive.dir / name).write_text(layout, encoding="utf-8")
        except OSError as exc:
            print(f"[capture] could not store the built temple: {exc}")
            return

        response = dict(self._pending_responses.pop((dungeon_id, floor), None) or {})
        # The response was written before the temple existed, so it states no
        # shape. The submission says what was actually built.
        for field, source in (
            ("dungeonLayoutType", "dungeonLayoutType"),
            ("dungeonFloorType", "dungeonFloorType"),
            ("dungeonFloorTotalCount", "dungeonFloorCount"),
            ("dungeonSettingsIndex", "dungeonSettingsIndex"),
        ):
            value = req.get(source)
            if isinstance(value, int) and value > 0:
                response[field] = value
        response.setdefault("dungeonID", dungeon_id)
        response.setdefault("dungeonFloorNumber", floor)

        self.archive.record_local_temple(
            dungeon_id, floor, response, name, origin="client-online"
        )
        print(f"[capture] banked temple {dungeon_id} floor {floor}")

    def _bank_captured_run(self, req: dict[str, Any]) -> None:
        """Store the player's own run as a phantom on that temple."""
        dungeon_id = int(_first(req, "dungeonId", "dungeonID") or 0)
        floor = int(_first(req, "dungeonFloorNumber", "floorIndex") or 0)
        run_data = _first(req, "runData", "runInfo")
        if dungeon_id <= 0 or not isinstance(run_data, str) or not run_data:
            return
        attempt = int(_first(req, "routeAttemptId", "routeAttemptID") or 0)
        if self._store_run(req, dungeon_id, floor, attempt, run_data):
            print(f"[capture] banked your run on {dungeon_id} floor {floor}")

    def _note_minted(self, dungeon_id: int, *, locked: bool = False) -> None:
        """Remember that this id is ours, not the live service's.

        Offline ids are drawn from the same range the real service uses -- it
        has reached 747,840 and keeps climbing, so most synthetic ids land
        inside the occupied band. Nothing in the id distinguishes them, so the
        only reliable record is made here, at the moment we invent one.

        `locked` says the caller already holds the state lock.
        """
        def remember() -> None:
            minted = self.state.setdefault("mintedDungeons", [])
            if dungeon_id not in minted:
                minted.append(int(dungeon_id))
                self._save()

        if locked:
            remember()
        else:
            with self._lock:
                remember()

    def _origin_of(self, dungeon_id: int) -> str:
        """Where a temple came from, for the moment its layout is stored.

        Capture mode forwards to the live service rather than minting, so an id
        this server invented can only have come from offline play.
        """
        minted = self.state.get("mintedDungeons") or []
        return "client-offline" if int(dungeon_id) in minted else "client-online"

    def _pinned_temple(self, floor: int):
        """The temple this server forces everyone onto, if one is set."""
        if self.archive is None:
            return None
        pin = self.state.get("pinnedDungeon")
        if not pin:
            return None
        temple = self.archive.lookup(int(pin), floor)
        if temple is not None and temple.layout and temple.response:
            return temple
        return None

    def pin_temple(self, dungeon_id: int | None) -> None:
        with self._lock:
            if dungeon_id:
                self.state["pinnedDungeon"] = int(dungeon_id)
            else:
                self.state.pop("pinnedDungeon", None)
            self._save()

    def _daily_block(self) -> dict[str, Any]:
        """`dailyDungeonInfo` / `dailySubmissionResponse`.

        Captured verbatim when available -- the live server returns the current
        daily state on every run submission, not just daily ones.
        """
        captured = self.archive.user_response if self.archive else None
        if isinstance(captured, dict):
            block = captured.get("dailyDungeonInfo")
            if isinstance(block, dict):
                return dict(block)
        return dict(DAILY_STUB)

    def _dungeon_record(self, req: dict[str, Any], relic: Any) -> dict[str, Any]:
        """One dungeon inside a route entry, matching a captured completion.

        Whip unlocks are keyed on `dungeonID` -- the client resolves them with
        `GetUnlockedWhipByDungeonID`, and no whip is ever named in the response.
        Preserving this record is therefore what preserves an unlocked whip.
        """
        dungeon_id = int(_first(req, "dungeonId", "dungeonID") or 0)
        # The in-game id, not whatever the request happened to carry. A run
        # banked under 0 names nobody, and the collector of a relic is the
        # player who collected it.
        user_id = _first(req, "userId", "userID") or self._caller_id(req) or 0
        floors = self.state.get("dungeonFloors", {}).get(str(dungeon_id))
        return {
            "dungeonID": dungeon_id,
            # 0 here, and that is not an oversight: a captured lastRunRouteInfo
            # returned straight after a live run carries 0 too. The id is
            # filled in when the route is banked, not when it is handed back.
            "routeID": 0,
            "routeStage": int(req.get("routeStage") or 0),
            "numGhosts": 0,
            "numFloors": int(
                floors or req.get("dungeonFloorCount") or 0
            ),
            "settingsIndex": int(req.get("dungeonSettingsIndex") or 0),
            "relicIDs": [relic] if relic else None,
            "relicCollectionFlags": 0,
            "relicCollectorUserIDs": [user_id] if relic else None,
            "relicCollectorNames": [None] if relic else None,
            # -1 while the route is being handed back, 2 once it is history.
            # A live capture shows both, which is how they came to be confused.
            "attemptedStatus": -1,
            "areaID": int(
                self.state.get("dungeonAreas", {}).get(str(dungeon_id))
                or req.get("areaID")
                or 0
            ),
        }

    def _route_info_for(self, req: dict[str, Any]) -> dict[str, Any]:
        """The RouteInfo the real server returns once a route ends."""
        relic = req.get("collectedRelicId")
        return {
            "routeID": int(_first(req, "routeId", "routeID") or 0),
            "gameMode": GAME_MODES.get(req.get("gameMode"), 0),
            "shareCode": None,
            # The player who finished it, named the same way every other
            # response names them. All 55 carried a real id.
            "completedByID": _first(req, "userId", "userID") or self._caller_id(req) or 0,
            "completedByName": None,
            "routeAttemptCount": 0,
            "routeAttemptID": _first(req, "routeAttemptId", "routeAttemptID") or 0,
            "dungeonVersion": SERVER_VERSION,
            "curseLevel": int(
                req.get("curseLevel")
                or self.state.get("routeCurses", {}).get(
                    str(_first(req, "routeId", "routeID") or 0)
                )
                or 0
            ),
            "wageredWhipID": self.state.get("routeWhips", {}).get(
                str(_first(req, "routeId", "routeID") or 0)
            ),
            "purchased": 0,
            "sandBag": int(req.get("isSandbag") or 0),
            # What the run was holding. Cleared when the route is banked.
            "storedCurrency": req.get("currency"),
            "dungeons": [self._dungeon_record(req, relic)],
        }

    @staticmethod
    def _as_history(route: dict[str, Any]) -> dict[str, Any]:
        """The same route, in the shape the server keeps rather than returns.

        A live capture shows the two are not the same. Handed back in
        lastRunRouteInfo a route carries dungeons[].routeID 0, attemptedStatus
        -1 and whatever currency the run was holding. Read back out of
        victoryRoutes at the next login, the same route names its own id,
        says 2, and has no stored currency -- measured across 55 of them with
        no exceptions.

        Banking the returned shape leaves a dungeon pointing at no route at
        all, which is what the client walked in circles on.
        """
        banked = dict(route)
        banked["storedCurrency"] = None
        banked["dungeons"] = [
            dict(d, routeID=int(route.get("routeID") or 0), attemptedStatus=2)
            for d in (route.get("dungeons") or [])
        ]
        return banked

    def _store_run(
        self,
        req: dict[str, Any],
        dungeon_id: int,
        floor: int,
        attempt_id: int,
        run_data: Any,
    ) -> bool:
        """Save the phantom recording next to the archived ones."""
        if self.archive is None or not isinstance(run_data, str) or not run_data:
            return False
        if dungeon_id <= 0:
            return False

        # Name it so the existing archive indexer recognizes it as a phantom.
        name = f"local__dungeon-{dungeon_id}-floor-{floor}-runinfo-{attempt_id:06d}"
        try:
            (self.archive.dir / name).write_text(run_data, encoding="utf-8")
        except OSError:
            return False

        # A phantom carries who ran it, and that has to come from the runner's
        # own profile: the client sends no name at all on /SubmitRun, and the
        # `reputation` it does send is the route's earnings, not the runner's
        # standing. Real phantoms from the CDN carry a total (6596 in one
        # captured entry), so using the delta would show a stranger's whole
        # reputation as whatever their last route happened to be worth.
        runner = self.profiles.for_player(
            _first(req, "playerId", "playerID")
        ).snapshot()
        self.archive.record_local_run(
            dungeon_id,
            floor,
            name,
            {
                "runData": "",
                "userID": req.get("userId") or runner.get("userID") or 0,
                "userName": runner.get("currentUsername") or "Explorer",
                "success": req.get("success") or 0,
                "lifetime": req.get("lifetime") or 0.0,
                "endLocation": req.get("endLocation") or "",
                "runID": attempt_id,
                "reputation": runner.get("reputation") or 0,
                "platform": 1,
                # No platformUserID. It is the runner's SteamID64, nothing
                # reads it back, and on a server other people connect to it
                # turned the archive into a record of who had visited.
            },
        )
        return True

    # ------------------------------------------------------------- daily

    def _daily_info(self, day_offset: int) -> dict[str, Any]:
        day = (_utc() + timedelta(days=day_offset)).date()
        midnight = datetime(
            day.year, day.month, day.day, tzinfo=timezone.utc
        ) + timedelta(days=1)
        return {
            "expiryTime": _ue_time(midnight),
            "dungeonSeed": stable_seed("daily", day.isoformat()),
            "leaderboard": [],
            "clearanceRate": 0.0,
        }

    def daily_info(self, _req: dict[str, Any]) -> dict[str, Any]:
        return {
            "serverStatus": self._maintenance(),
            "today": self._daily_info(0),
            "yesterday": self._daily_info(-1),
        }

    # ---------------------------------------------------------- economy

    def purchase_upgrade(self, req: dict[str, Any]) -> dict[str, Any]:
        """`/PurchaseUpgrade` -> deducts what the client offered, persists it.

        Shape mirrors a captured response: `result`, `purchaseItemID`, `userID`,
        `spentEssence`, `spentKeys`, `currency`, `upgrades`.
        """
        return self._profile_for(req).purchase_upgrade(req)

    def purchase_temporary_power(self, req: dict[str, Any]) -> dict[str, Any]:
        out = self._profile_for(req).spend_currency(req)
        out["powerIndex"] = int(req.get("purchaseID") or req.get("powerIndex") or 0)
        return out

    def convert_keys(self, req: dict[str, Any]) -> dict[str, Any]:
        # The client states both sides of the trade, so no rate table is
        # needed. Captured exchanges were 10:1 upward
        # (spendAmount 280 -> requestedAmount 28, then 10 -> 1).
        spend = int(req.get("spendAmount") or 0)
        want = int(req.get("requestedAmount") or req.get("amount") or 0)
        source = int(req.get("sourceKeyType") or 0)
        up = bool(req.get("convertUp", True))
        rate = max(1, spend // want) if want else 1
        return self._profile_for(req).convert_keys(
            {
                **req,
                "amount": want,
                "rate": rate,
                "fromKeyIndex": source,
                "toKeyIndex": source + 1 if up else max(0, source - 1),
            }
        )

    def spend_currency(self, req: dict[str, Any]) -> dict[str, Any]:
        out = self._profile_for(req).spend_currency(req)
        out["purchaseItemID"] = req.get("purchaseID") or req.get("purchaseItemId") or 0
        out["spentEssence"] = req.get("essences", 0)
        out["spentKeys"] = req.get("keys", [])
        return out

    # ----------------------------------------------------------- social

    def friends(self, _req: dict[str, Any]) -> dict[str, Any]:
        return {"friends": []}

    def generic_ok(self, _req: dict[str, Any]) -> dict[str, Any]:
        return {"serverStatus": self._maintenance()}
