"""Reporting and blacklisting of unplayable temples.

Phantom Abyss can produce temples that cannot be finished. The developers have
acknowledged it: a door with no reachable key, an exit walled off, a relic
placed out of bounds. Once the servers are gone nobody is left to fix the
generator, so an offline server has to route around them instead.

Two different things can be broken, and they need different handling:

**Generated temples.** The client builds these from a seed. Nothing is stored
upstream, so a bad one is cured by handing out a different seed -- the player
gets a fresh temple in the same slot. What gets blacklisted is not the seed on
its own but the whole *generation fingerprint*: the same seed in a different
area, floor shape or settings collection builds a completely different temple,
so seed alone would blacklist temples that are perfectly fine.

**Archived temples.** These are real layout blobs pulled from the CDN while the
servers were up. There is no seed to reroll -- the layout is fixed bytes. A bad
one can only be skipped, so it is keyed by dungeon id and floor.

Reports are portable. The fingerprint is derived from the same values every
server sends, so a report made on one machine identifies the same temple on
every other machine, and `export` / `import_reports` let players pool findings.

Trust
-----
A report is a claim, not a fact. Plenty of temples merely *look* impossible to
a player who has not found the route yet, and a blacklist that accepts every
claim would quietly delete good content for everyone downstream.

So local and imported reports are trusted differently:

* Reported here, by a player on this server -- blocked immediately. They hit
  it, and re-serving it to them helps nobody.
* Imported from someone else -- blocked once ``IMPORT_THRESHOLD`` independent
  reporters agree, or when somebody has marked it ``confirmed``.
* ``cleared`` always wins. It is the escape hatch for a temple that was
  reported in error, and no amount of agreement overrides it.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

# How many independent reporters a report needs before this server acts on it,
# unless the operator made it themselves. One stranger's word is not enough to
# withhold content.
IMPORT_THRESHOLD = 2

# Where a report came from, which is what decides how far it is trusted.
SOURCE_LOCAL = "local"      # the person running this server, on their machine
SOURCE_GUEST = "guest"      # somebody playing on a server they do not own
SOURCE_IMPORT = "import"    # a file from somebody else entirely

# How many separate temples one reporter may condemn before this server stops
# counting their word. A player who genuinely cannot finish a temple reports a
# handful over a lifetime; a player reporting scores of them is either broken
# or malicious, and either way their votes should not decide what everyone
# else gets to play. Their reports are kept -- an operator can still read them
# and confirm one by hand -- they simply stop counting toward the threshold.
MAX_TEMPLES_PER_REPORTER = 25

# How many different seeds to try before giving up and serving the last one.
# A blacklist dense enough to exhaust this is a bug in the blacklist.
MAX_REROLLS = 16

STATUS_REPORTED = "reported"
STATUS_CONFIRMED = "confirmed"
STATUS_CLEARED = "cleared"

# Why a temple could not be finished. Free text lives in `note`; these exist so
# reports stay sortable once there are thousands of them.
REASONS = {
    "missing-key": "a door needs a key that never spawned",
    "unreachable-exit": "the exit cannot be reached",
    "unreachable-relic": "the relic cannot be reached",
    "softlock": "the run can enter a state it cannot leave",
    "empty": "the temple generated with no rooms",
    "crash": "the temple crashes or hangs the client",
    "other": "something else -- see the note",
}

# The values the client generates a temple from. Two servers agreeing on all of
# these produce the same temple, which is what makes a report portable.
# `dungeonFloorTotalCount` is deliberately absent. The real server reports 0
# for a temple it has just minted and the real count on every later request, so
# a temple reported at mint time and re-requested afterwards would fingerprint
# as two different temples and the report would never take effect.
FINGERPRINT_FIELDS = (
    "gameMode",
    "areaID",
    "dungeonSettingsIndex",
    "dungeonLayoutType",
    "dungeonFloorType",
    "difficultyRating",
    "dungeonSeed",
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Fingerprint:
    """The generation inputs that decide what a temple looks like."""

    gameMode: int
    areaID: int
    dungeonSettingsIndex: int
    dungeonLayoutType: int
    dungeonFloorType: int
    difficultyRating: int
    dungeonSeed: int

    @classmethod
    def from_response(cls, response: dict[str, Any]) -> "Fingerprint":
        return cls(**{f: int(response.get(f) or 0) for f in FINGERPRINT_FIELDS})

    @property
    def id(self) -> str:
        joined = "|".join(f"{f}={getattr(self, f)}" for f in FINGERPRINT_FIELDS)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> str:
        area = {1: "Ruins", 2: "Caverns", 3: "Inferno", 4: "Rift"}.get(
            self.areaID, f"area {self.areaID}"
        )
        return (
            f"seed {self.dungeonSeed} in {area}, "
            f"layout {self.dungeonLayoutType}/{self.dungeonFloorType}, "
            f"settings {self.dungeonSettingsIndex}"
        )


@dataclass
class Report:
    id: str
    kind: str  # "generated" | "archived"
    reason: str
    status: str = STATUS_REPORTED
    note: str = ""
    fingerprint: dict[str, int] | None = None
    dungeonID: int | None = None
    floor: int | None = None
    reporters: list[str] | None = None
    firstSeen: str = ""
    lastSeen: str = ""
    source: str = "local"

    @property
    def count(self) -> int:
        return len(self.reporters or [])

    def blocks(self) -> bool:
        """Whether this server should withhold the temple.

        The operator's own word is enough on their own machine -- it is their
        server and they were there. Everybody else needs corroboration, which
        is the whole defense against one player condemning every temple they
        are served: a lone report is recorded and shown, and withholds nothing
        until somebody else runs the same temple and agrees.
        """
        if self.status == STATUS_CLEARED:
            return False
        if self.status == STATUS_CONFIRMED:
            return True
        if self.source == SOURCE_LOCAL:
            return True
        return self.count >= IMPORT_THRESHOLD

    def describe(self) -> str:
        if self.kind == "archived":
            what = f"archived dungeon {self.dungeonID} floor {self.floor}"
        elif self.fingerprint:
            what = Fingerprint(**self.fingerprint).describe()
        else:
            what = "unknown temple"
        return f"{self.id}  {self.reason:<18} {what}"


class Blacklist:
    """Reports of unplayable temples, and the decision to withhold them."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data = self._load()

    # ------------------------------------------------------------- storage

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text("utf-8"))
                if isinstance(data, dict) and "reports" in data:
                    data.setdefault("salt", self._new_salt())
                    return data
            except (json.JSONDecodeError, OSError):
                # A corrupt blacklist must never stop the server serving
                # temples. Start clean and keep the bad file for inspection.
                broken = self.path.with_suffix(".corrupt")
                try:
                    self.path.replace(broken)
                    print(f"[blacklist] unreadable, moved to {broken.name}")
                except OSError:
                    pass
        return {"version": SCHEMA_VERSION, "salt": self._new_salt(), "reports": {}}

    @staticmethod
    def _new_salt() -> str:
        return hashlib.sha256(os.urandom(32)).hexdigest()[:32]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------ identity

    def pseudonym(self, player_id: Any) -> str:
        """A stable, non-reversible stand-in for a player id.

        Reports get shared, and a SteamID64 is a real person's account. Salting
        per install means an export cannot be walked back to an account. The
        cost is that the same player reporting from two installs counts twice
        when those exports are merged; over-counting agreement is the safer
        direction to be wrong in than leaking an identity.
        """
        raw = f"{self._data['salt']}|{player_id or 'anonymous'}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:12]

    # ------------------------------------------------------------- reading

    def noisy_reporters(self) -> set[str]:
        """Reporters whose word has stopped counting, and why it must.

        A player who cannot finish a temple reports a handful in a lifetime.
        One reporting scores of them is broken or malicious, and on a server
        other people play on, their word would otherwise decide what everybody
        gets. Their reports are still recorded and still shown to the operator
        -- only their vote toward the threshold is dropped.
        """
        seen: dict[str, int] = {}
        for raw in self._data["reports"].values():
            for reporter in raw.get("reporters") or []:
                seen[reporter] = seen.get(reporter, 0) + 1
        return {who for who, n in seen.items() if n > MAX_TEMPLES_PER_REPORTER}

    def _hydrate(self, raw: dict[str, Any], discount: set[str]) -> Report:
        """A report as this server counts it, rather than as stored.

        The file keeps every reporter so an operator can audit it; what comes
        back here has the discounted ones removed, so `count` and `blocks()`
        answer the question actually being asked -- how many people whose word
        we still take have said this.
        """
        if discount and raw.get("reporters"):
            raw = dict(raw)
            raw["reporters"] = [r for r in raw["reporters"] if r not in discount]
        return Report(**raw)

    def reports(self) -> list[Report]:
        discount = self.noisy_reporters()
        return [self._hydrate(r, discount)
                for r in self._data["reports"].values()]

    def get(self, report_id: str) -> Report | None:
        raw = self._data["reports"].get(report_id)
        return self._hydrate(raw, self.noisy_reporters()) if raw else None

    def blocks_fingerprint(self, fp: Fingerprint) -> bool:
        report = self.get(fp.id)
        return report.blocks() if report else False

    def blocks_temple(self, dungeon_id: int, floor: int) -> bool:
        report = self.get(archived_id(dungeon_id, floor))
        return report.blocks() if report else False

    # ------------------------------------------------------------- writing

    def _upsert(
        self, report_id: str, defaults: dict[str, Any], reporter: str
    ) -> Report:
        existing = self._data["reports"].get(report_id)
        if existing is None:
            existing = dict(defaults, id=report_id, reporters=[], firstSeen=_now())
            self._data["reports"][report_id] = existing
        reporters = existing.setdefault("reporters", [])
        if reporter not in reporters:
            reporters.append(reporter)
        existing["lastSeen"] = _now()
        # A fresh report on a cleared temple reopens it rather than being lost.
        if existing.get("status") == STATUS_CLEARED:
            existing["status"] = STATUS_REPORTED
        return Report(**existing)

    def report_generated(
        self,
        fp: Fingerprint,
        *,
        reason: str = "other",
        note: str = "",
        player_id: Any = None,
        source: str = SOURCE_LOCAL,
    ) -> Report:
        report = self._upsert(
            fp.id,
            {
                "kind": "generated",
                "reason": reason,
                "note": note,
                "fingerprint": asdict(fp),
                "status": STATUS_REPORTED,
                "source": source,
            },
            self.pseudonym(player_id),
        )
        self.save()
        return report

    def report_archived(
        self,
        dungeon_id: int,
        floor: int,
        *,
        reason: str = "other",
        note: str = "",
        player_id: Any = None,
        source: str = SOURCE_LOCAL,
    ) -> Report:
        report = self._upsert(
            archived_id(dungeon_id, floor),
            {
                "kind": "archived",
                "reason": reason,
                "note": note,
                "dungeonID": int(dungeon_id),
                "floor": int(floor),
                "status": STATUS_REPORTED,
                "source": source,
            },
            self.pseudonym(player_id),
        )
        self.save()
        return report

    def set_status(self, report_id: str, status: str) -> Report | None:
        raw = self._data["reports"].get(report_id)
        if raw is None:
            return None
        raw["status"] = status
        raw["lastSeen"] = _now()
        self.save()
        return Report(**raw)

    def remove(self, report_id: str) -> bool:
        if self._data["reports"].pop(report_id, None) is None:
            return False
        self.save()
        return True

    # --------------------------------------------------------- interchange

    def export(self) -> dict[str, Any]:
        """A shareable copy. Carries no player ids, salted or otherwise."""
        out = []
        for raw in self._data["reports"].values():
            entry = {k: v for k, v in raw.items() if k != "reporters"}
            entry["count"] = len(raw.get("reporters") or [])
            out.append(entry)
        return {
            "version": SCHEMA_VERSION,
            "reports": sorted(out, key=lambda r: r["id"]),
        }

    def import_reports(
        self, payload: dict[str, Any], *, origin: str = ""
    ) -> tuple[int, int]:
        """Merge someone else's export. Returns (added, merged).

        Imported reports keep ``source: community`` so they face the higher
        agreement bar in `Report.blocks`. Reporter counts add up, because two
        exports describing the same temple are two independent findings.

        `origin` is what makes that true. The stand-in reporter ids used to be
        derived from the report id alone, so a second contribution reporting
        the same temple produced the same ids and merged into the first --
        leaving one voice where there were two, and a temple that never
        reached the threshold no matter how many people agreed.

        Naming them by where they came from fixes both halves: two
        contributions count twice, and importing the same one again counts
        once. With no origin given, the payload's own content stands in, so
        re-importing a file is still idempotent.
        """
        if not origin:
            origin = hashlib.sha256(
                json.dumps(payload.get("reports") or [], sort_keys=True).encode()
            ).hexdigest()[:12]
        added = merged = 0
        for entry in payload.get("reports") or []:
            report_id = entry.get("id")
            if not report_id:
                continue
            incoming = max(1, int(entry.get("count") or 1))
            existing = self._data["reports"].get(report_id)
            if existing is None:
                self._data["reports"][report_id] = {
                    "id": report_id,
                    "kind": entry.get("kind", "generated"),
                    "reason": entry.get("reason", "other"),
                    "note": entry.get("note", ""),
                    "fingerprint": entry.get("fingerprint"),
                    "dungeonID": entry.get("dungeonID"),
                    "floor": entry.get("floor"),
                    "status": entry.get("status", STATUS_REPORTED),
                    "reporters": [f"import:{origin}:{i}" for i in range(incoming)],
                    "firstSeen": entry.get("firstSeen") or _now(),
                    "lastSeen": entry.get("lastSeen") or _now(),
                    "source": "community",
                }
                added += 1
                continue
            # Never let an import silently un-clear a local decision.
            if existing.get("status") == STATUS_CLEARED:
                continue
            reporters = existing.setdefault("reporters", [])
            for i in range(incoming):
                tag = f"import:{origin}:{i}"
                if tag not in reporters:
                    reporters.append(tag)
            if entry.get("status") == STATUS_CONFIRMED:
                existing["status"] = STATUS_CONFIRMED
            existing["lastSeen"] = _now()
            merged += 1
        self.save()
        return added, merged


def archived_id(dungeon_id: int, floor: int) -> str:
    """Report id for a fixed archived temple, which has no seed to reroll."""
    raw = f"archived|{int(dungeon_id)}|{int(floor)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def unblocked_seed(
    blacklist: "Blacklist | None",
    seed_of: Any,
    response_template: dict[str, Any],
    *,
    start: int = 0,
) -> tuple[int, int]:
    """Find a seed whose temple is not blacklisted.

    ``seed_of(n)`` derives the nth candidate seed; ``response_template`` carries
    every other generation input. Returns ``(seed, reroll)`` so the caller can
    persist which reroll it landed on -- a rerolled temple has to stay stable
    across restarts, or the player's temple changes under them mid-run.
    """
    reroll = start
    seed = seed_of(reroll)
    if blacklist is None:
        return seed, reroll
    for _ in range(MAX_REROLLS):
        candidate = Fingerprint.from_response(
            dict(response_template, dungeonSeed=seed)
        )
        if not blacklist.blocks_fingerprint(candidate):
            return seed, reroll
        reroll += 1
        seed = seed_of(reroll)
    print("[blacklist] no unblocked seed found; serving the last candidate")
    return seed, reroll


def summarize(reports: Iterable[Report]) -> str:
    reports = list(reports)
    if not reports:
        return "no reports"
    blocking = sum(1 for r in reports if r.blocks())
    by_reason: dict[str, int] = {}
    for r in reports:
        by_reason[r.reason] = by_reason.get(r.reason, 0) + 1
    detail = ", ".join(f"{n} {reason}" for reason, n in sorted(by_reason.items()))
    return f"{len(reports)} report(s), {blocking} blocking -- {detail}"


def send_report(
    base_url: str,
    *,
    player_id: Any,
    dungeon_id: int,
    floor: int,
    reason: str = "other",
    note: str = "",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Tell a server you do not own that one of its temples cannot be finished.

    The other half of `OfflineBackend.report_temple`. A player's own copy of
    this program calls it; the game never does, and has no idea this endpoint
    exists.

    The server decides what to do with it -- a guest's report withholds nothing
    until somebody else agrees -- so the answer is worth showing the player
    rather than assuming it landed.
    """
    import json as _json
    import urllib.error
    import urllib.request

    body = _json.dumps({
        "playerId": player_id,
        "dungeonId": int(dungeon_id),
        "dungeonFloorNumber": int(floor),
        "reason": reason,
        "note": note,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/offline/report", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return _json.loads(answer.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"accepted": False, "reason": f"could not reach the server: {exc}"}
