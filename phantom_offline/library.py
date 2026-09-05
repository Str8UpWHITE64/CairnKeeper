"""Serves archived temples and phantoms back to the game.

The archive holds the real CDN blobs, named by their original URL path::

    version_128__2024-02-23__dungeon-701203-floor-2-layout-ygck77l0
    version_128__2026-04-27__dungeon-701203-floor-2-runinfo-lttxkd70

That naming carries everything needed to index them: dungeon id, floor number,
and whether the blob is a temple layout or a phantom recording. This module
turns the flat asset directory into a lookup so the offline backend can answer
``/GetDungeon`` with a genuine layout and a genuine set of phantoms rather than
an empty temple.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# dungeon-<id>-floor-<n>-layout-<slug>  /  ...-runinfo-<slug>
BLOB_RE = re.compile(
    r"dungeon-(?P<dungeon>\d+)-floor-(?P<floor>\d+)-(?P<kind>layout|runinfo)-(?P<slug>\w+)"
)


@dataclass
class Temple:
    dungeon_id: int
    floor: int
    layout: str | None = None
    ghosts: list[str] = field(default_factory=list)
    ghost_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The real /GetDungeon response for this temple, if we captured one.
    response: dict[str, Any] | None = None
    # Where this temple came from. "cdn" was downloaded from wiby.net;
    # "client-online" was built by a client while connected to the live service,
    # so its id is real and the temple is genuine content; "client-offline" was
    # built against our own server and its id means nothing outside this
    # machine. Only the first two are safe to contribute to a shared pool.
    origin: str = "cdn"

    @property
    def game_mode(self) -> int | None:
        """Which mode this temple belongs to (0 Abyss, 1 Adventure, 2 Daily).

        Modes are not interchangeable: an Adventure temple has 3 floors while an
        Abyss or Daily one has 2. Serving the wrong kind hands the client a
        floor count it is not expecting.
        """
        if not self.response:
            return None
        mode = self.response.get("gameMode")
        return mode if isinstance(mode, int) else None

    @property
    def floor_count(self) -> int | None:
        if not self.response:
            return None
        total = self.response.get("dungeonFloorTotalCount")
        return total if isinstance(total, int) and total > 0 else None

    def layout_url(self, base_url: str) -> str:
        return f"{base_url}/asset/{self.layout}" if self.layout else ""

    def replay(self, base_url: str) -> dict[str, Any] | None:
        """The captured response with every CDN URL pointed back at us.

        Replaying beats synthesising: the seed, floor type, layout type and
        guardian all came from the real server and are known-good for this
        exact temple.
        """
        if self.response is None:
            return None
        out = dict(self.response)
        out["layoutDownloadURL"] = self.layout_url(base_url)
        out["ghostRuns"] = self.ghost_runs(base_url)

        # A temple built on this server has no visitor history from upstream,
        # so the captured counts are zero and the game reports that nobody has
        # ever been here -- while showing the player a ghost. We know exactly
        # who ran it, because we recorded each run.
        #
        # An archived temple keeps the real server's numbers: those count every
        # player worldwide, far more than the ~50 phantoms ever served, and
        # replacing them with our phantom count would understate them wildly.
        if not out.get("playerCount") and self.ghosts:
            out["playerCount"] = len(self.ghosts)
            out["deathCount"] = sum(
                1
                for name in self.ghosts
                if (self.ghost_meta.get(name) or {}).get("success") == 0
            )
        return out

    def ghost_runs(self, base_url: str) -> list[dict[str, Any]]:
        """Rebuild the `ghostRuns` array, pointing downloads at ourselves.

        Every phantom must arrive with an id of its own. The client names the
        actor it spawns after the runner, so two phantoms sharing an id are two
        actors sharing a name, and the second one takes the whole game down::

            LowLevelFatalError [Line: 417] An actor of name 'Ghost0' already
            exists in level 'Main_Temple'

        Pooled recordings have no ids at all -- a contribution deliberately
        carries no account fields -- so `Ghost{userID}` resolved to `Ghost0` for
        all of them. The same crash was already reachable without pooling: five
        floors in a real 1,652-floor archive hold two recordings by the same
        player, who ran that floor twice, and both spawn as `Ghost62842`. Rather than shipping somebody's real account id to put a
        number back, give each recording a stable one derived from the blob it
        already points at: unique per phantom, identical on every request so a
        client that remembers one still recognizes it, and attached to nobody.
        """
        runs = []
        taken: set[str] = set()
        for name in self.ghosts:
            meta = dict(self.ghost_meta.get(name, {}))
            meta["runData"] = ""
            meta["downloadURL"] = f"{base_url}/asset/{name}"
            meta.setdefault("userName", "Explorer")
            meta.setdefault("success", 2)
            meta.setdefault("lifetime", 0.0)
            meta.setdefault("platform", 1)
            # The client expects this field, so it keeps its place -- but it is
            # the runner's SteamID64, and serving it hands every player in a
            # temple the platform account of everyone else who ran it.
            meta["platformUserID"] = ""
            stand_in = _stand_in_id(name)
            if not meta.get("userID") or str(meta["userID"]) in taken:
                # Either it never had an id, or somebody already on this floor
                # has that one. The second case is a player who ran the same
                # floor twice -- both recordings are real and worth keeping, but
                # they cannot both spawn under the same name.
                meta["userID"] = stand_in
            taken.add(str(meta["userID"]))
            if not meta.get("runID"):
                # Offset so a run id never collides with the user id beside it.
                meta["runID"] = stand_in + 1
            runs.append(meta)
        return runs


def _stand_in_id(asset_name: str) -> int:
    """A stable, unique, meaningless id for a recording that has none.

    Derived from the asset name, which for pooled content is its content hash,
    so the same recording keeps the same id across restarts and across the
    people who hold it. Kept clear of the range the real service uses -- its
    ids were in the tens of millions -- so a stand-in is recognisable as one.
    """
    digest = hashlib.sha256(asset_name.encode("utf-8")).digest()
    return 900_000_000 + (int.from_bytes(digest[:6], "big") % 90_000_000) * 2


# What the server names a layout the client built for itself, when we had no
# archived one to hand over. See `submit_layout` in backend.py.
GENERATED_LAYOUT_SUFFIX = "-layout-generated"


def is_generated_layout(name: "str | None") -> bool:
    """Whether this layout was built by a client rather than served by the CDN."""
    return bool(name) and str(name).endswith(GENERATED_LAYOUT_SUFFIX)


class Library:
    """Indexes the archived blobs by (dungeon id, floor)."""

    def __init__(
        self,
        assets_dir: Path,
        fixtures_dir: Path | None = None,
        bundles: "list[Path] | None" = None,
    ):
        self.dir = assets_dir
        self.fixtures = fixtures_dir
        # Read-only content somebody else gathered. Indexed after the player's
        # own, so anything they captured themselves wins: their copy of a
        # temple carries their phantoms and their history with it.
        self.bundles = [Path(b) for b in (bundles or [])]
        self.temples: dict[tuple[int, int], Temple] = {}
        # The captured /VerifyUserID response, replayed so the client sees its
        # real account (whips, upgrades, progression) offline.
        self.user_response: dict[str, Any] | None = None
        # Newest captured login per account, keyed by userID.
        self.user_responses: dict[int, dict[str, Any]] = {}
        # A captured login from an account that had never played, used as the
        # template for a player this server has never seen.
        self.first_login: dict[str, Any] | None = None
        # Last captured 200 response per endpoint, for generic replay.
        self.responses: dict[str, dict[str, Any]] = {}
        self._homes: dict[str, Path] = {}
        # Archived filename by its URL tail, built once when the captured
        # responses are read. See _stored_name for why.
        self._names_by_tail: dict[str, str] | None = None
        self.local_runs_path = assets_dir / "local_runs.json"
        self.local_temples_path = assets_dir / "local_temples.json"
        # Offline generation writes layouts and phantoms here, and in offline
        # mode no AssetArchiver exists to have created it.
        try:
            assets_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        for bundle in self.bundles:
            if bundle.exists():
                self._index_assets(bundle)
        if assets_dir.exists():
            self._index_assets()
            self._index_local_runs()
            self._index_local_temples()
        if fixtures_dir is not None and fixtures_dir.exists():
            self._index_ghost_metadata(fixtures_dir)
            self._index_user(fixtures_dir)
            self._index_responses(fixtures_dir)

    def _index_local_runs(self) -> None:
        """Restore phantoms recorded on this machine in earlier sessions."""
        if not self.local_runs_path.exists():
            return
        try:
            entries = json.loads(self.local_runs_path.read_text("utf-8"))
        except (ValueError, OSError):
            return
        for entry in entries:
            try:
                self.add_local_ghost(
                    int(entry["dungeonID"]),
                    int(entry["dungeonFloorNumber"]),
                    entry["file"],
                    entry["meta"],
                )
            except (KeyError, TypeError, ValueError):
                continue

    def record_local_run(
        self, dungeon_id: int, floor: int, name: str, meta: dict[str, Any]
    ) -> None:
        """Persist a locally recorded phantom so it survives a restart."""
        self.add_local_ghost(dungeon_id, floor, name, meta)
        entries = []
        if self.local_runs_path.exists():
            try:
                entries = json.loads(self.local_runs_path.read_text("utf-8"))
            except (ValueError, OSError):
                entries = []
        entries = [e for e in entries if e.get("file") != name]
        entries.append(
            {
                "dungeonID": dungeon_id,
                "dungeonFloorNumber": floor,
                "file": name,
                "meta": meta,
            }
        )
        tmp = self.local_runs_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        tmp.replace(self.local_runs_path)

    def _index_responses(self, fixtures: Path) -> None:
        """Keep the last successful response for each captured endpoint.

        Replaying a real response beats synthesising one: fields we never
        thought to populate (reward tables, rate arrays) come along for free.
        """
        for path in fixtures.glob("*.jsonl"):
            endpoint = path.stem.lower()
            for line in path.read_text("utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                body = entry.get("response_body")
                if entry.get("status") == 200 and isinstance(body, dict):
                    self.responses[endpoint] = body

    def captured(self, endpoint: str) -> dict[str, Any] | None:
        return self.responses.get(endpoint.lower())

    def _index_user(self, fixtures: Path) -> None:
        """Index captured logins per account, keeping the newest of each.

        Keyed on `userID`, which is a plain integer and survives redaction --
        unlike `playerId`, which is pseudonymised in the archive. Without this
        the last login captured wins globally, so a second account logging in
        would shadow the first player's profile snapshot.
        """
        source = fixtures / "VerifyUserID.jsonl"
        if not source.exists():
            return
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
            self.user_response = body  # newest overall, for compatibility
            user_id = body.get("userID")
            if isinstance(user_id, int):
                self.user_responses[user_id] = body
            # What the live service sends someone who has never connected.
            if self.first_login is None and body.get("existingUser") is False:
                self.first_login = body

    def login_for_name(self, name: Any) -> dict[str, Any] | None:
        """The captured login belonging to the account with this display name.

        Captures redact the platform id, so the name is what identifies whose
        save a captured login is. Anyone else asking gets nothing.
        """
        wanted = str(name or "").strip()
        if not wanted:
            return None
        for body in self.user_responses.values():
            if str(body.get("currentUsername") or "").strip() == wanted:
                return body
        return None

    def new_player_login(self) -> dict[str, Any] | None:
        """A captured first connection, replayed for someone we have never met.

        Better than a response assembled from guesses: it is exactly what the
        live service sent an account that had never played, so every field the
        client reads is present and shaped the way the client expects. The
        identity in it belongs to whoever was captured, and the caller must
        replace it with the player's own.
        """
        return self.first_login

    def latest_login(self, user_id: Any) -> dict[str, Any] | None:
        try:
            return self.user_responses.get(int(user_id))
        except (TypeError, ValueError):
            return None

    def latest_progression(self, user_id: Any) -> dict[str, Any]:
        """The newest currency and reputation the live server stated.

        A login is only current until the next run finishes. `/SubmitRun`
        reports the banked totals at route end, and `/PurchaseUpgrade` and
        `/ConvertKeys` report them after spending -- so the newest statement of
        an account's standing is usually not the login at all.

        Seeding a profile from the login alone leaves it stale by however many
        runs followed, which for a player still going online is every session.
        """
        try:
            wanted = int(user_id)
        except (TypeError, ValueError):
            return {}

        newest: dict[str, Any] = {}
        seen_at: dict[str, str] = {}
        if self.fixtures is None or not self.fixtures.exists():
            return newest
        for path in sorted(self.fixtures.glob("*.jsonl")):
            for line in path.read_text("utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("status") != 200:
                    continue
                response = row.get("response_body")
                if not isinstance(response, dict):
                    continue
                # The response must name its own owner. Falling back to the
                # request's userId looks harmless -- we made the request, so it
                # is us -- but plenty of endpoints answer a question about
                # somebody else. A share-list reply carries the reputation of
                # the player being looked up, and adopting it silently wrote a
                # stranger's standing into this profile.
                if response.get("userID") != wanted:
                    continue
                stamp = str(row.get("ts") or "")
                currency = response.get("currency")
                if currency and stamp >= seen_at.get("currency", ""):
                    newest["currency"] = currency
                    seen_at["currency"] = stamp
                reputation = response.get("reputation")
                if reputation is None:
                    reputation = response.get("updatedReputation")
                if reputation and stamp >= seen_at.get("reputation", ""):
                    newest["reputation"] = reputation
                    seen_at["reputation"] = stamp
        newest["_seenAt"] = seen_at
        return newest

    def census(self) -> dict:
        """Three honest counts, because one number here misleads.

        A run through a temple is recorded once per floor, and 93% of the
        players on any floor are the same people carrying on from the last one.
        So the file count is real -- every recording is needed to replay its
        own floor, none is redundant -- but reading it as "different ghosts"
        roughly doubles the truth. Measured here: 23,064 recordings were 12,128
        journeys by 3,355 people.
        """
        recordings = runs = 0
        people: set = set()
        journeys: set = set()
        for (dungeon, _), temple in self.temples.items():
            for name in temple.ghosts:
                recordings += 1
                who = (temple.ghost_meta.get(name) or {}).get("userID")
                if who:
                    people.add(who)
                    journeys.add((dungeon, who))
        runs = len(journeys) or recordings
        return {
            "recordings": recordings,
            "runs": runs,
            "people": len(people),
            "temples": len(self.temples),
            "complete": len(self.complete_dungeons()),
        }

    def poolable(self) -> "list[tuple[int, int]]":
        """Temples that may be contributed to a shared pool.

        Anything this server minted an id for is excluded. Its id occupies the
        same range the live service uses, so once uploaded there is no way to
        tell it from a real temple carrying that id, and one would displace the
        other. A synthetic temple also has no value to anyone else: they can
        generate their own.
        """
        return sorted(
            key
            for key, temple in self.temples.items()
            if temple.origin in ("cdn", "client-online")
            and temple.layout
            and temple.response
        )

    def complete_dungeons(self) -> list[int]:
        """Dungeon ids whose every floor is archived and replayable.

        A partially archived temple is worse than useless for a fresh run: the
        player would reach a floor we cannot serve and the run would dead-end.
        """
        by_dungeon: dict[int, list[Temple]] = {}
        for (dungeon, _), temple in self.temples.items():
            by_dungeon.setdefault(dungeon, []).append(temple)

        out = []
        for dungeon, temples in by_dungeon.items():
            usable = [t for t in temples if t.layout and t.response]
            if not usable:
                continue
            expected = self.floors_expected(dungeon)
            if expected is None or len(usable) >= expected:
                out.append(dungeon)
        return sorted(out)

    def abyss_temple(
        self,
        area: int,
        floor_total: int,
        *,
        pick: int = 0,
        exclude: "set[int] | frozenset[int]" = frozenset(),
    ) -> int | None:
        """A complete archived Abyss temple for this area, if one is spare.

        Abyss never reached the substitution path: its dungeon id comes from
        the route position rather than being asked for, so every offline Abyss
        run generated four empty temples while thousands of archived phantoms
        sat unused.

        Matching is on area *and* floor count together. A route is 2/2/2/2
        floors on Master and 3/3/3/3 on Nightmare, so dropping a three-floor
        temple into a two-floor slot would leave the player a floor short of
        the exit.

        Returns the dungeon id to adopt, so the route records it and later
        floors resolve to the same temple.
        """
        by_dungeon: dict[int, list[Temple]] = {}
        for (dungeon, _), temple in self.temples.items():
            if temple.game_mode != 0 or not (temple.layout and temple.response):
                continue
            if temple.response.get("areaID") != area:
                continue
            if temple.response.get("dungeonFloorTotalCount") != floor_total:
                continue
            by_dungeon.setdefault(dungeon, []).append(temple)

        usable = sorted(
            dungeon
            for dungeon, floors in by_dungeon.items()
            if len(floors) >= floor_total and dungeon not in exclude
        )
        if not usable:
            return None
        return usable[pick % len(usable)]

    def any_for_floor(
        self,
        floor: int,
        *,
        pick: int = 0,
        game_mode: int | None = None,
        difficulty: int | None = None,
        exclude: "set[int] | frozenset[int]" = frozenset(),
    ) -> Temple | None:
        """An archived temple that has this floor.

        Used when the client starts a fresh route and asks for a dungeon we
        have never seen: rather than hand back an empty temple, give it a real
        archived one. Later floors arrive carrying that dungeon id and resolve
        exactly, so only floor 0 ever substitutes.

        `pick` rotates the choice so offline play is not always the same
        temple. Only fully archived temples are offered.
        """
        complete = self.complete_dungeons()
        candidates = [
            self.temples[(d, floor)]
            for d in complete
            if (d, floor) in self.temples
            and d not in exclude
            and self.temples[(d, floor)].layout
            and self.temples[(d, floor)].response
        ]
        if game_mode is not None:
            # Never substitute across modes. An Adventure temple has three
            # floors and layoutType 3; an Abyss one has two and layoutType 1.
            # Handing the client the wrong kind crashes it outright -- an
            # Adventure request answered with an Abyss temple killed the game
            # on level load. Returning nothing is safe: the caller then hands
            # out a seed and the client builds a temple of the right kind.
            candidates = [t for t in candidates if t.game_mode == game_mode]
            if not candidates:
                return None
        if difficulty is not None:
            # Never substitute across difficulties either, for the same reason
            # and on stronger evidence: across 2,428 captured exchanges the
            # real server answered the difficulty it was asked for every single
            # time, with no exceptions. Fresh runs map it straight onto the
            # area -- 0 to area 1, 20 to 2, 40 to 3, 60 to 4.
            #
            # Substituting across it hands a player starting a fresh run an
            # area 4 temple built for difficulty 60. That is roughly a quarter
            # of what this archive can offer, and the one time it happened the
            # phantoms walked through the walls and the game crashed two floors
            # later.
            # Only temples that state a different difficulty are refused. One
            # that states none is left alone: this is meant to stop a known
            # mismatch being served, not to start withholding temples nobody
            # has ever had trouble with.
            matching = [
                t for t in candidates
                if (t.response or {}).get("difficultyRating") in (None, difficulty)
            ]
            if not matching:
                return None
            candidates = matching
        if not candidates:
            # Nothing complete has this floor; fall back to anything usable.
            candidates = [
                t
                for (_, f), t in sorted(self.temples.items())
                if f == floor and t.layout and t.response
            ]
        if not candidates:
            return None
        return candidates[pick % len(candidates)]

    def _index_assets(self, directory: "Path | None" = None) -> None:
        directory = directory or self.dir
        for path in directory.iterdir():
            if not path.is_file() or path.name == "index.json":
                continue
            match = BLOB_RE.search(path.name)
            if not match:
                continue
            key = (int(match["dungeon"]), int(match["floor"]))
            temple = self.temples.setdefault(key, Temple(*key))
            if match["kind"] == "layout":
                temple.layout = path.name
            elif path.name not in temple.ghosts:
                temple.ghosts.append(path.name)
            # Remember which directory holds it, so it can be served without
            # searching and without a bundle being able to name a file outside
            # itself.
            self._homes[path.name] = directory

    def _index_ghost_metadata(self, fixtures: Path) -> None:
        """Recover each phantom's name, time and outcome from captured responses.

        A phantom is not just a recording -- the game shows who it was and how
        they did. That metadata only exists in the `ghostRuns` array, so it is
        harvested from the fixtures rather than the blobs.
        """
        source = fixtures / "GetDungeon.jsonl"
        if not source.exists():
            return
        for line in source.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            body = entry.get("response_body")
            if not isinstance(body, dict) or entry.get("status") != 200:
                continue

            # Keep the whole response so it can be replayed verbatim.
            dungeon = body.get("dungeonID")
            floor = body.get("dungeonFloorNumber")
            if isinstance(dungeon, int) and isinstance(floor, int):
                temple = self.temples.get((dungeon, floor))
                if temple is not None:
                    temple.response = body

            for run in body.get("ghostRuns") or []:
                if not isinstance(run, dict):
                    continue
                url = run.get("downloadURL") or ""
                match = BLOB_RE.search(url)
                if not match:
                    continue
                name = url.rsplit("/", 1)[-1]
                key = (int(match["dungeon"]), int(match["floor"]))
                temple = self.temples.get(key)
                if temple is None:
                    continue
                stored = self._stored_name(name)
                if stored:
                    temple.ghost_meta[stored] = run

    def _stored_name(self, url_tail: str) -> str | None:
        """Which archived file holds what this URL points at.

        Looked up rather than searched. This walked every filename in the
        archive for every phantom in every captured response -- around 23,000
        lookups across 25,000 names, which is some 575 million comparisons and
        took thirteen seconds on a real archive. It runs on a worker thread,
        but a worker holding the interpreter that long makes the window itself
        stutter, which is what it looked like from outside.

        An archived name is the URL path with the separators replaced, so the
        part after the last one is the tail being asked for.
        """
        index = self._names_by_tail
        if index is None:
            index = {}
            for candidate in self.temples_files():
                index.setdefault(candidate.rsplit("__", 1)[-1], candidate)
            self._names_by_tail = index
        found = index.get(url_tail)
        if found is not None:
            return found
        # A name that does not split the usual way still has to be found.
        for candidate in self.temples_files():
            if candidate.endswith(url_tail):
                return candidate
        return None

    def temples_files(self) -> list[str]:
        names: list[str] = []
        for temple in self.temples.values():
            if temple.layout:
                names.append(temple.layout)
            names.extend(temple.ghosts)
        return names

    # ------------------------------------------------------------ public

    def record_local_temple(
        self,
        dungeon_id: int,
        floor: int,
        response: dict[str, Any],
        layout_name: str | None = None,
        origin: str = "client-offline",
    ) -> None:
        """Register a temple this server generated rather than archived.

        When no archived layout exists, the backend hands the client a seed and
        an empty ``layoutDownloadURL``. The client then builds the temple itself
        and uploads it via ``/SubmitDungeonLayout``. Storing both halves here
        makes that temple permanent and replayable, which is how an offline
        server keeps producing new content after the CDN is gone.
        """
        temple = self.temples.get((dungeon_id, floor))
        if temple is None:
            temple = Temple(dungeon_id, floor)
            self.temples[(dungeon_id, floor)] = temple
        temple.response = response
        temple.origin = origin
        if layout_name:
            temple.layout = layout_name

        entries = self._read_json(self.local_temples_path, [])
        entries = [
            e
            for e in entries
            if not (
                e.get("dungeonID") == dungeon_id
                and e.get("dungeonFloorNumber") == floor
            )
        ]
        entries.append(
            {
                "dungeonID": dungeon_id,
                "dungeonFloorNumber": floor,
                "layout": temple.layout,
                "response": response,
                "origin": origin,
            }
        )
        self._write_json(self.local_temples_path, entries)

    def _index_local_temples(self) -> None:
        for entry in self._read_json(self.local_temples_path, []):
            try:
                dungeon_id = int(entry["dungeonID"])
                floor = int(entry["dungeonFloorNumber"])
            except (KeyError, TypeError, ValueError):
                continue
            temple = self.temples.get((dungeon_id, floor)) or Temple(dungeon_id, floor)

            # A layout the client generated must never displace one the CDN
            # served. Every phantom on this floor was recorded running through
            # the archived building; hand the client a different building and
            # all of them walk through its walls -- which is what happened, on
            # floors 0 and 1 of a temple whose floor 2 still had the real one.
            #
            # The layout and the response go together or not at all. The
            # response carries the seed and the floor types that belong to that
            # building, and taking one from each source is how the two came
            # apart in the first place.
            if is_generated_layout(entry.get("layout")) and (
                    temple.layout and not is_generated_layout(temple.layout)):
                continue

            temple.response = entry.get("response")
            # Records written before provenance existed are assumed synthetic.
            # Being wrong in that direction withholds a real temple from the
            # pool; being wrong the other way contaminates it.
            temple.origin = entry.get("origin") or "client-offline"
            if entry.get("layout"):
                temple.layout = entry["layout"]
            self.temples[(dungeon_id, floor)] = temple

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            return default

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)

    def add_local_ghost(
        self, dungeon_id: int, floor: int, name: str, meta: dict[str, Any]
    ) -> None:
        """Register a run recorded on this machine as a phantom.

        Called when the player finishes a run offline, so their own ghost shows
        up on the next visit the way it would have online. Registered live
        rather than at startup so it appears without restarting the server.
        """
        temple = self.temples.get((dungeon_id, floor))
        if temple is None:
            temple = Temple(dungeon_id, floor)
            self.temples[(dungeon_id, floor)] = temple
        if name not in temple.ghosts:
            temple.ghosts.append(name)
        temple.ghost_meta[name] = meta

    def lookup(self, dungeon_id: int, floor: int) -> Temple | None:
        return self.temples.get((dungeon_id, floor))

    def by_mode(self) -> dict[int | None, list[int]]:
        """Complete dungeon ids grouped by game mode."""
        out: dict[int | None, list[int]] = {}
        for dungeon in self.complete_dungeons():
            temple = next(
                (t for (d, _), t in self.temples.items() if d == dungeon and t.response),
                None,
            )
            out.setdefault(temple.game_mode if temple else None, []).append(dungeon)
        return {k: sorted(v) for k, v in out.items()}

    def layout_type_for(self, dungeon_id: int) -> int | None:
        """The layoutType this temple's archived floors actually use.

        Most archived temples are missing their final floor -- the real server
        only ever produced one once a player reached it -- so that floor gets
        generated offline instead. It has to be generated in the *temple's* own
        style, not the style implied by the mode on the request: Abyss temples
        are layoutType 1 throughout, Adventure 3, and handing the generator a
        combination that does not occur produces an empty temple (see the
        black-screen note in backend.floor_shape).
        """
        for (dungeon, _), temple in self.temples.items():
            if dungeon != dungeon_id or not temple.response:
                continue
            value = temple.response.get("dungeonLayoutType")
            if isinstance(value, int) and value > 0:
                return value
        return None

    def floors_expected(self, dungeon_id: int) -> int | None:
        """How many floors this temple has, per the server's own response.

        Needed to tell a fully archived temple from one where only the floors
        reached so far were captured.
        """
        for (dungeon, _), temple in self.temples.items():
            if dungeon != dungeon_id or not temple.response:
                continue
            total = temple.response.get("dungeonFloorTotalCount")
            if isinstance(total, int) and total > 0:
                return total
        return None

    def asset_path(self, name: str) -> Path | None:
        """Resolve an asset name, without letting it escape where it lives.

        A name is only ever resolved inside the directory it was indexed from,
        so a bundle cannot name a file belonging to the player -- or to
        anywhere else on their machine.
        """
        homes = [self._homes.get(name)] if name in self._homes else []
        for home in homes + [self.dir]:
            if home is None:
                continue
            candidate = (home / name).resolve()
            try:
                candidate.relative_to(home.resolve())
            except ValueError:
                continue  # path traversal attempt
            if candidate.is_file():
                return candidate
        return None

    def summary(self) -> str:
        layouts = sum(1 for t in self.temples.values() if t.layout)
        ghosts = sum(len(t.ghosts) for t in self.temples.values())
        line = f"{len(self.temples)} temple(s), {layouts} layout(s), {ghosts} phantom(s)"
        live = [b for b in self.bundles if b.exists()]
        if live:
            line += f" (including {len(live)} shared bundle(s))"
        return line
