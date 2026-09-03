"""Archives fresh temples straight from the live server.

How this works
--------------
Tracing a real play session showed the loop the client follows::

    GetDungeon(routeId=0, dungeonId=0, floor=0)   -> server mints a new route
                                                     and a new temple
    SubmitRun(...)                                -> that floor was finished
    GetDungeon(routeId=R, dungeonId=D, floor=1)   -> next floor of that temple
    ...
    GetDungeon(routeId=R, dungeonId=0, floor=0, routeStage=N+1) -> next temple

Two things make automation possible, both confirmed against the live server:

1. ``routeId=0, dungeonId=0`` mints a brand new route and temple every time.
2. Later floors can be requested **without submitting a run first**.

Point 2 matters. Advancing via ``SubmitRun`` would mean posting fabricated
completions to a live service: inventing leaderboard entries, publishing fake
phantoms for other players to race against, and corrupting the account's own
statistics. None of that is necessary -- walking the floors directly archives
exactly the same data and claims nothing that did not happen.

What this does still do is create one route record per temple, the same thing
pressing "start run" does. That is the endpoint's ordinary use, but it is not
free, so the harvester is rate limited and takes a bounded temple count.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .assets import AssetArchiver, iter_urls
from .library import Library
from .session import SessionStore
from .upstream import Forwarder, UpstreamError

GAMESERVICE = "https://gameservice.wiby.net"
ENDPOINT = f"{GAMESERVICE}/GetDungeon"

# Slow down, then stop, rather than pushing through these.
BACKOFF_STATUSES = {429, 500, 502, 503, 504}

# Safety rail: a temple has a handful of floors, never hundreds.
MAX_FLOORS_PER_TEMPLE = 8

# Abyss difficulty tiers. Master and Nightmare are confirmed from traffic;
# Insane and Unchained are inferred from their position in the menu.
ABYSS_TIERS = {"master": 0, "insane": 1, "nightmare": 2, "unchained": 3}

# An Abyss route spans four dungeons, one per area, and only the first is
# reachable without submitting runs: asking for the next stage returns 400.
# So an Abyss harvest captures area 1 of each route -- 2 of 8 floors on
# Master, 3 of 12 on Nightmare.
ABYSS_REACHABLE_STAGES = 1

# Modes worth harvesting. DGM_DAILY is excluded on purpose -- there is exactly
# one daily temple at a time, so it cannot be harvested in bulk; capture it by
# playing. DGM_PRACTISE and DGM_ERRORMODE are not real content.
# DGM_CLASSIC is what the menu calls **Abyss Mode** -- an 8-floor descent
# through Ruins/Caverns/Inferno/Rift. It is the only mode that yields areas
# above 1, so it is worth harvesting separately from Adventure.
HARVESTABLE_MODES = ("DGM_ADVENTURE", "DGM_CLASSIC")

# `difficultyRating` selects which area a fresh Adventure route lands in, and
# at the top tier how long the temple is. Measured against the live server:
#
#     diff   area   floors   note
#        0      1        3
#       20      2        3
#       40      3        3
#       60      4        3
#       80      4        4   deepest valid tier
#      100+     0        0   empty placeholder, never useful
#
# Values that are not multiples of 20 also return the empty placeholder.
#
# This is the clean route to deep-area content: no route progression and no
# fabricated run submissions, and the temples returned are established ones
# carrying ~50 phantoms each.
DIFFICULTY_TIERS = (0, 20, 40, 60, 80)
MAX_DIFFICULTY = 80
AREA_DIFFICULTY = {1: 0, 2: 20, 3: 40, 4: 60}


# A pool that has run dry hands back freshly minted temples: areaID 0, one
# floor, no players, no phantoms, with strictly sequential dungeon ids. They
# archive nothing and each one creates a junk route on the live server, so a
# run of them means stop -- not keep going. Learned the hard way: 102
# consecutive empties before anyone noticed.
EMPTY_STREAK_LIMIT = 5


def is_empty_temple(page: dict) -> bool:
    return (
        not (page.get("dungeonFloorTotalCount") or 0)
        or not (page.get("playerCount") or 0)
    )


@dataclass
class Result:
    temples: int = 0
    floors: int = 0
    blobs: int = 0
    failures: int = 0
    empty: int = 0
    stopped_early: str | None = None


class Harvester:
    def __init__(
        self,
        state_dir: Path,
        *,
        delay: float = 2.0,
        max_failures: int = 4,
        timeout: float = 60.0,
        mode: str = "DGM_ADVENTURE",
        difficulty: int = 0,
        curse_level: int = 0,
        area: int | None = None,
    ):
        if area is not None:
            if area not in AREA_DIFFICULTY:
                raise ValueError(
                    f"area must be one of {sorted(AREA_DIFFICULTY)}, got {area}"
                )
            difficulty = AREA_DIFFICULTY[area]
        elif difficulty not in DIFFICULTY_TIERS:
            raise ValueError(
                f"difficultyRating {difficulty} returns an empty placeholder "
                f"temple. Valid tiers: {', '.join(map(str, DIFFICULTY_TIERS))}"
            )
        self.state_dir = state_dir
        self.mode = mode
        # `difficultyRating` is the Abyss difficulty tier; `curseLevel` is what
        # the UI calls Challenge Level. Both are sent, but see the note in
        # run(): neither has been observed to change which temple is returned.
        self.difficulty = difficulty
        self.curse_level = curse_level
        self.area = area
        self.delay = delay
        self.max_failures = max_failures
        self.timeout = timeout
        self.session = SessionStore(state_dir / "session")
        self.archiver = AssetArchiver(state_dir / "assets")
        # Requests go through the Forwarder so every response lands in
        # fixtures/ the same way a captured one does. Without that the blobs
        # arrive but the temple is not replayable: the seed, floor types and
        # phantom metadata all live in the response, not in the blobs.
        self.forwarder = Forwarder(state_dir / "fixtures", archiver=self.archiver)
        self.log_path = state_dir / "harvest_log.jsonl"
        self.consecutive_failures = 0

    # ------------------------------------------------------------- request

    def _get_dungeon(
        self,
        creds: dict[str, Any],
        *,
        route_id: int,
        dungeon_id: int,
        floor: int,
        stage: int = 0,
    ) -> tuple[int, dict[str, Any] | None]:
        body = dict(creds)
        body.update(
            {
                "userId": creds.get("userId") or 0,
                "gameMode": self.mode,
                "dungeonSettingsIndex": 0,
                "routeId": route_id,
                "routeStage": stage,
                "dungeonFloorNumber": floor,
                "dungeonId": dungeon_id,
                "shareCode": "",
                "knownDungeons": [],
                "whipWagered": "Bamboo",
                "isRetry": False,
                "difficultyRating": self.difficulty,
                "curseLevel": self.curse_level,
                "networkFilter": False,
                "platformFriends": [],
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
        except UpstreamError:
            return 0, None
        if status != 200:
            return status, None
        try:
            return status, json.loads(data)
        except ValueError:
            return status, None

    # -------------------------------------------------------------- record

    def _note(self, entry: dict[str, Any]) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def _handle_failure(self, status: int) -> None:
        self.consecutive_failures += 1
        if status in BACKOFF_STATUSES:
            wait = min(self.delay * (2**self.consecutive_failures), 60.0)
            print(f"    status {status}; backing off {wait:.0f}s")
            time.sleep(wait)

    # ----------------------------------------------------------------- run

    def run(
        self, *, count: int, on_temple: Callable[[int], None] | None = None
    ) -> Result:
        creds = self.session.load()
        if not creds or not self.session.is_complete():
            raise RuntimeError(
                "no usable credentials. Run `serve --capture`, start the game and "
                "reach the main menu, then try again."
            )

        result = Result()
        empty_streak = 0
        for index in range(1, count + 1):
            if empty_streak >= EMPTY_STREAK_LIMIT:
                result.stopped_early = (
                    f"{empty_streak} empty temples in a row -- this pool is "
                    "drained. Try another area or difficulty, or come back later."
                )
                break
            if self.consecutive_failures >= self.max_failures:
                result.stopped_early = (
                    f"{self.consecutive_failures} consecutive failures"
                )
                break

            status, first = self._get_dungeon(creds, route_id=0, dungeon_id=0, floor=0)
            if status != 200 or not first:
                result.failures += 1
                print(f"[{index}/{count}] new route -> {status}")
                self._handle_failure(status)
                continue

            self.consecutive_failures = 0
            if is_empty_temple(first):
                empty_streak += 1
                result.empty += 1
                print(
                    f"[{index}/{count}] empty temple "
                    f"{first.get('dungeonID')} (streak {empty_streak})"
                )
                time.sleep(self.delay)
                continue
            empty_streak = 0
            route_id = int(first.get("routeID") or 0)
            dungeon_id = int(first.get("dungeonID") or 0)
            total = int(first.get("dungeonFloorTotalCount") or 1)
            total = max(1, min(total, MAX_FLOORS_PER_TEMPLE))

            # The Forwarder already queued these; count them for the report.
            queued = len(list(iter_urls(first)))
            result.temples += 1
            result.floors += 1
            print(
                f"[{index}/{count}] {self.mode} area {first.get('areaID')} "
                f"dungeon {dungeon_id} "
                f"(route {route_id}, {total} floors): floor 0 ok, {queued} blob(s)"
            )
            self._note(
                {
                    "dungeonID": dungeon_id,
                    "routeID": route_id,
                    "floor": 0,
                    "floors": total,
                    "mode": self.mode,
                    "seed": first.get("dungeonSeed"),
                }
            )

            for floor in range(1, total):
                time.sleep(self.delay)
                status, page = self._get_dungeon(
                    creds, route_id=route_id, dungeon_id=dungeon_id, floor=floor
                )
                if status != 200 or not page:
                    result.failures += 1
                    print(f"           floor {floor} -> {status}")
                    self._handle_failure(status)
                    break
                self.consecutive_failures = 0
                queued = len(list(iter_urls(page)))
                result.floors += 1
                print(f"           floor {floor} ok, {queued} blob(s)")
                self._note(
                    {
                        "dungeonID": dungeon_id,
                        "routeID": route_id,
                        "floor": floor,
                        "floors": total,
                        "mode": self.mode,
                        "seed": page.get("dungeonSeed"),
                    }
                )

            if on_temple is not None:
                on_temple(dungeon_id)
            time.sleep(self.delay)

        print("\nwaiting for blob downloads to finish...")
        self.archiver.drain()
        result.blobs = self.archiver.fetched
        return result


def archive_size(state_dir: Path) -> tuple[int, float]:
    """(file count, megabytes) currently in the asset archive."""
    assets = state_dir / "assets"
    if not assets.exists():
        return 0, 0.0
    files = [p for p in assets.iterdir() if p.is_file() and p.suffix != ".json"]
    return len(files), sum(p.stat().st_size for p in files) / 1_048_576


def complete_count(state_dir: Path) -> int:
    library = Library(state_dir / "assets", state_dir / "fixtures")
    return len(library.complete_dungeons())
