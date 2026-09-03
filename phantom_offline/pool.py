"""Pooling what players kept, so no one has to have kept all of it.

No single player will ever have run enough temples. A few hundred between them
might, and each of them gathered theirs simply by playing. This is the format
that lets them combine: a contribution is a directory of blobs plus a manifest
naming them by content hash.

Deliberately a file rather than a service. A folder can be zipped and handed to
someone today, which works with no server, no accounts and no hosting bill; a
central pool later is the same format with the transport automated. Building the
service first would have made the format an afterthought, and the format is the
part that has to be right.

What travels
------------
Temple layouts, phantom recordings, and the response describing each temple.
Nothing else. No profile, no player id, no credentials -- the state directory
holds a live Steam auth ticket, and it must be structurally impossible to
include rather than merely discouraged.

What does not travel
--------------------
Anything this server minted. Offline dungeon ids are drawn from the same range
the live service uses -- it reached 747,840 and kept climbing, while ours spread
across 100,000 to 1,000,000 -- so a synthetic temple uploaded to a pool is
indistinguishable from a real one carrying that id, and one would displace the
other. `origin` records which is which at the moment of creation, and only
`cdn` and `client-online` may be contributed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import shutil
from binascii import Error as BinasciiError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = 1
MANIFEST = "manifest.json"
BLOBS = "blobs"

# Fields worth keeping about a temple. Everything else in a captured response
# either belongs to the player who captured it or means nothing to anyone else.
TEMPLE_FIELDS = (
    "dungeonID", "dungeonFloorNumber", "gameMode", "areaID",
    "dungeonSeed", "dungeonLayoutType", "dungeonFloorType",
    "dungeonFloorTotalCount", "dungeonSettingsIndex", "difficultyRating",
    "dungeonVersion", "health", "temporaryPowerIndex",
)

# Phantom metadata worth keeping. `userName` is the runner's display name, and
# it travels by default: the game prints it above every phantom you run past, so
# it is already public to everyone who played, and stripping it turns a temple
# full of people into a temple full of "Explorer". The names are most of what
# makes a shared archive feel like the game rather than a pile of recordings.
#
# The identifiers below are a different matter, and they are not optional.
# `userID` and `runID` are the game's own numbering, not the platform's. They
# are what makes a phantom a person across a run: without them every recording
# is a stranger, and the client cannot tell that the Shrim on floor 2 is the
# Shrim who got you through floor 1. They also give each spawned actor a name of
# its own -- see Temple.ghost_runs, where the absence of them crashed the game.
# `platformUserID` is the SteamID64 and is deliberately absent from this list.
PHANTOM_FIELDS = ("success", "lifetime", "endLocation", "platform",
                  "userID", "runID")
PHANTOM_NAME_FIELD = "userName"

# What a recording carries that must never leave this machine.
#
# A recording is the JSON the client posted when it submitted a run, stored and
# served back verbatim, so it still holds the fields that submission needed:
# playerId is a SteamID64 -- paste one after steamcommunity.com/profiles/ and
# you are looking at the person -- and verificationToken is the Steam auth
# ticket they signed it with. In this archive, 24 of 200 sampled downloaded
# recordings carried a stranger's SteamID64 and 37 carried a distinct token.
# None of it is read back during playback; all of it identifies somebody.
#
# The values are blanked rather than the keys removed, so the shape the client
# parses is exactly the shape it has always parsed. Blanking is idempotent, so a
# recording that has already traveled hashes the same on the way out again.
IDENTITY_FIELDS = ("playerId", "verificationToken", "platformVerificationId")


def scrub_recording(raw: bytes) -> bytes | None:
    """Strip the submitter's identity out of a run recording.

    Returns the cleaned bytes in the same envelope they arrived in, or None if
    the recording cannot be read. Unreadable means unprovable, and a blob that
    cannot be shown to be clean does not travel.
    """
    encoded = False
    try:
        record = json.loads(raw)
    except ValueError:
        try:
            record = json.loads(base64.b64decode(raw, validate=True))
            encoded = True
        except (ValueError, BinasciiError):
            return None
    if not isinstance(record, dict):
        return None

    for key in IDENTITY_FIELDS:
        if key in record:
            record[key] = "" if isinstance(record[key], str) else 0

    body = json.dumps(record, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(body) if encoded else body


def carries_identity(raw: bytes) -> bool:
    """Whether a recording still names somebody. Used at both ends."""
    try:
        record = json.loads(raw)
    except ValueError:
        try:
            record = json.loads(base64.b64decode(raw, validate=True))
        except (ValueError, BinasciiError):
            return False        # opaque: refused elsewhere, not smuggled through
    if not isinstance(record, dict):
        return False
    return any(record.get(k) not in (None, "", 0, "0") for k in IDENTITY_FIELDS)


# A SteamID64 is 7656119 followed by ten digits.
_PLATFORM_ID_SHAPED = re.compile(r"^\s*7656119\d{10}\s*$")


def _safe_name(name: Any) -> str:
    """A display name, unless the display name is an account.

    Names travel because the game already shows them above every phantom you
    run past. Some players are called by their SteamID64 -- found in a real
    contribution by grepping a running server for `7656119` -- and a name that
    is an account is an account, whatever field it arrives in. Every promise
    made about this format says no platform ids travel, so it does not.
    """
    text = str(name or "")
    return "Explorer" if _PLATFORM_ID_SHAPED.match(text) else text


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Contribution:
    temples: list[dict] = field(default_factory=list)
    # sha256 -> a file to copy, or the exact bytes to write. Recordings are
    # scrubbed before they are hashed, so what the manifest names is the cleaned
    # content and never the copy sitting in the archive.
    blobs: dict[str, "Path | bytes"] = field(default_factory=dict)
    unreadable: int = 0      # recordings held back because they would not parse
    # Which of these temples somebody could not finish. Traveling with the
    # content rather than in a separate file is the point: a temple and the
    # knowledge that it is broken are no use apart, and a recipient who gets
    # the first without the second spends a run finding out for themselves.
    reports: list[dict] = field(default_factory=list)
    # Who this contribution is, so a recipient can tell two people reporting
    # the same temple from the same person sending twice. Deriving that from
    # the content instead very nearly worked: two players reporting the same
    # temple in the same second produce identical bytes and collapse into one.
    id: str = field(default_factory=lambda: secrets.token_hex(8))

    @property
    def bytes(self) -> int:
        total = 0
        for source in self.blobs.values():
            if isinstance(source, bytes):
                total += len(source)
            elif source.exists():
                total += source.stat().st_size
        return total

    def manifest(self, *, names: bool) -> dict:
        return {
            "schema": SCHEMA,
            "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "includesRunnerNames": names,
            "contribution": self.id,
            "temples": self.temples,
            "reports": self.reports,
        }


def build(state_dir: Path, *, include_names: bool = True) -> Contribution:
    """Gather everything this player could contribute.

    Only temples the live service minted. A temple this server invented has
    nothing to offer anyone -- they can generate their own -- and would collide
    with a real id if it traveled.
    """
    from .library import Library

    state_dir = Path(state_dir)
    library = Library(state_dir / "assets", state_dir / "fixtures")
    out = Contribution()

    # Reports carry no reporter identities -- the blacklist strips them on
    # export and keeps only how many people agreed.
    try:
        from .blacklist import Blacklist

        out.reports = Blacklist(
            state_dir / "state" / "blacklist.json"
        ).export().get("reports") or []
    except Exception:  # noqa: BLE001 - an archive with no reports is normal
        out.reports = []

    for (dungeon, floor) in library.poolable():
        temple = library.temples[(dungeon, floor)]
        layout_path = library.asset_path(temple.layout) if temple.layout else None
        if layout_path is None:
            continue

        response = {
            key: temple.response.get(key)
            for key in TEMPLE_FIELDS
            if temple.response and temple.response.get(key) is not None
        }

        layout_hash = digest(layout_path)
        out.blobs[layout_hash] = layout_path

        phantoms = []
        for name in temple.ghosts:
            path = library.asset_path(name)
            if path is None:
                continue
            cleaned = scrub_recording(path.read_bytes())
            if cleaned is None:
                # Cannot read it, so cannot prove it carries nobody's ticket.
                out.unreadable += 1
                continue
            meta = dict(temple.ghost_meta.get(name) or {})
            entry = {k: meta[k] for k in PHANTOM_FIELDS if k in meta}
            if include_names and meta.get(PHANTOM_NAME_FIELD):
                entry[PHANTOM_NAME_FIELD] = _safe_name(meta[PHANTOM_NAME_FIELD])
            entry["sha256"] = hashlib.sha256(cleaned).hexdigest()
            entry["bytes"] = len(cleaned)
            out.blobs[entry["sha256"]] = cleaned
            phantoms.append(entry)

        out.temples.append({
            "dungeonID": dungeon,
            "floor": floor,
            "origin": temple.origin,
            "response": response,
            "layout": {"sha256": layout_hash, "bytes": layout_path.stat().st_size},
            "phantoms": phantoms,
        })

    return out


def summary(state_dir: Path) -> dict:
    """What could be contributed, without hashing a single byte.

    Counting is not weighing. Hashing 6.4 GB to answer "how much is there"
    takes minutes and tells the player nothing they could not be told
    instantly, so status walks the index and leaves the files alone.
    """
    from .library import Library

    state_dir = Path(state_dir)
    library = Library(state_dir / "assets", state_dir / "fixtures")

    temples = phantoms = 0
    total = 0
    for (dungeon, floor) in library.poolable():
        temple = library.temples[(dungeon, floor)]
        layout = library.asset_path(temple.layout) if temple.layout else None
        if layout is None:
            continue
        temples += 1
        total += layout.stat().st_size
        for name in temple.ghosts:
            path = library.asset_path(name)
            if path is not None:
                phantoms += 1
                total += path.stat().st_size
    return {"temples": temples, "phantoms": phantoms, "bytes": total}


def export(state_dir: Path, out_dir: Path, *, include_names: bool = True,
           have: set[str] | None = None) -> dict:
    """Write a contribution somebody else can read.

    `have` is the set of hashes the recipient already holds. Passing it turns a
    full export into the difference, which is the whole reason for hashing:
    two players who ran the same temple send it once between them.
    """
    contribution = build(state_dir, include_names=include_names)
    out_dir = Path(out_dir)
    blob_dir = out_dir / BLOBS
    blob_dir.mkdir(parents=True, exist_ok=True)

    have = have or set()
    copied = skipped = 0
    for sha, source in contribution.blobs.items():
        if sha in have:
            skipped += 1
            continue
        target = blob_dir / sha
        if not target.exists():
            if isinstance(source, bytes):
                target.write_bytes(source)
            else:
                shutil.copy2(source, target)
        copied += 1

    manifest = contribution.manifest(names=include_names)
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {
        "temples": len(contribution.temples),
        "reports": len(contribution.reports),
        "blobs": copied,
        "skipped": skipped,
        "bytes": sum((blob_dir / s).stat().st_size
                     for s in contribution.blobs if (blob_dir / s).exists()),
        "includesRunnerNames": include_names,
        "unreadable": contribution.unreadable,
    }


def wanted(state_dir: Path, manifest: dict) -> list[str]:
    """Which hashes in a manifest this archive does not already hold.

    The negotiation that keeps pooling cheap: offer hashes, receive a list,
    move only those.
    """
    from .library import Library

    state_dir = Path(state_dir)
    library = Library(state_dir / "assets", state_dir / "fixtures")
    held = set()
    for temple in library.temples.values():
        for name in ([temple.layout] if temple.layout else []) + list(temple.ghosts):
            path = library.asset_path(name)
            if path is not None:
                held.add(digest(path))

    asked = set()
    for temple in manifest.get("temples") or []:
        layout = (temple.get("layout") or {}).get("sha256")
        if layout:
            asked.add(layout)
        for phantom in temple.get("phantoms") or []:
            if phantom.get("sha256"):
                asked.add(phantom["sha256"])
    return sorted(asked - held)


@dataclass
class Ingest:
    accepted: int = 0
    rejected: int = 0
    phantoms: int = 0
    reports: int = 0      # findings about temples that cannot be finished
    reasons: dict[str, int] = field(default_factory=dict)

    def refuse(self, why: str) -> None:
        self.rejected += 1
        self.reasons[why] = self.reasons.get(why, 0) + 1


def _layout_is_readable(raw: bytes) -> bool:
    """A layout must decode into something the game could build."""
    try:
        text = raw.decode("utf-8").strip()
        data = json.loads(base64.b64decode(text))
    except Exception:  # noqa: BLE001
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return False
    return isinstance(data, dict) and "roomInfos" in data


def ingest(state_dir: Path, contribution_dir: Path) -> Ingest:
    """Take in somebody else's contribution, checking it at the door.

    Everything here arrived from a stranger, so nothing is assumed. A blob must
    hash to what the manifest claims, a layout must decode, and a temple must
    describe itself consistently -- a layout with no shape is exactly the
    corruption that crashed the game twice while this was being built.
    """
    from .library import Library

    state_dir = Path(state_dir)
    contribution_dir = Path(contribution_dir)
    result = Ingest()

    try:
        manifest = json.loads((contribution_dir / MANIFEST).read_text("utf-8"))
    except (OSError, ValueError):
        result.refuse("unreadable manifest")
        return result
    if manifest.get("schema") != SCHEMA:
        result.refuse("unknown schema")
        return result

    blob_dir = contribution_dir / BLOBS
    library = Library(state_dir / "assets", state_dir / "fixtures")
    assets = state_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    # Somebody else's findings about which temples cannot be finished. They are
    # recorded as imports, so they face the higher agreement bar: one
    # stranger's word withholds nothing here either.
    incoming_reports = manifest.get("reports") or []
    if incoming_reports:
        from .blacklist import Blacklist

        try:
            black = Blacklist(state_dir / "state" / "blacklist.json")
            added, merged = black.import_reports(
                {"reports": incoming_reports},
                origin=str(manifest.get("contribution") or ""),
            )
            black.save()
            result.reports = added + merged
        except (OSError, ValueError, TypeError, KeyError):
            # A malformed report must not cost the recipient the content, but
            # it must not pass silently either -- a bare `except` here hid this
            # very block being written into the wrong function.
            result.refuse("unreadable reports")

    for temple in manifest.get("temples") or []:
        dungeon = temple.get("dungeonID")
        floor = temple.get("floor")
        response = temple.get("response") or {}
        layout = temple.get("layout") or {}

        if not isinstance(dungeon, int) or not isinstance(floor, int):
            result.refuse("no temple id")
            continue
        if temple.get("origin") not in ("cdn", "client-online"):
            result.refuse("not from the live service")
            continue

        source = blob_dir / str(layout.get("sha256") or "")
        if not source.is_file():
            result.refuse("layout missing")
            continue
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != layout.get("sha256"):
            result.refuse("layout does not match its hash")
            continue
        if not _layout_is_readable(raw):
            result.refuse("layout will not decode")
            continue
        # A layout implies a shape. One without the other builds geometry the
        # client has no description for, and it dies on level load.
        if not (response.get("dungeonLayoutType") and response.get("dungeonFloorType")):
            result.refuse("layout without a shape")
            continue

        name = f"pool__dungeon-{dungeon}-floor-{floor}-layout-{layout['sha256'][:12]}"
        (assets / name).write_bytes(raw)
        library.record_local_temple(dungeon, floor, dict(response), name,
                                    origin=temple["origin"])
        result.accepted += 1

        for phantom in temple.get("phantoms") or []:
            sha = phantom.get("sha256")
            blob = blob_dir / str(sha or "")
            if not blob.is_file():
                continue
            data = blob.read_bytes()
            if hashlib.sha256(data).hexdigest() != sha:
                continue
            if carries_identity(data):
                # Somebody's SteamID64 or auth ticket, from a sender whose
                # client did not strip it. Not ours to store or pass on.
                result.refuse("recording still names its runner")
                continue
            ghost = f"pool__dungeon-{dungeon}-floor-{floor}-runinfo-{sha[:12]}"
            (assets / ghost).write_bytes(data)
            meta = {k: phantom[k] for k in PHANTOM_FIELDS if k in phantom}
            meta.setdefault("userName", phantom.get("userName") or "Explorer")
            library.record_local_run(dungeon, floor, ghost, meta)
            result.phantoms += 1

    return result
