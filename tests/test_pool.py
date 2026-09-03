"""Pooling must carry temples and nothing else.

No single player will ever have run enough temples; a few hundred between them
might. What travels is layouts, phantom recordings, and the response describing
each temple. What must never travel is anything belonging to the person who
sent it, or anything this server invented.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import pool  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

LAYOUT = base64.b64encode(
    json.dumps({"numWings": 2, "roomInfos": [{"roomIndex": 0}]}).encode()
).decode()
def recording(steam_id: str = "76561198000000042", *, token: str = "STEAM-TICKET",
              encoded: bool = True) -> bytes:
    """What the client actually posts when it submits a run.

    The identity fields are not incidental -- every recording in the real
    archive carries them, including the ones downloaded from other players.
    """
    body = json.dumps({
        "startPos": {"x": 1719.8, "y": -6598.0},
        "numRecordedFrames": 4210,
        "compressedLocations": "AAAA",
        "userId": 62842,
        "playerId": steam_id,
        "platform": "STEAM",
        "verificationToken": token,
        "platformVerificationId": "0110000123",
    }).encode()
    return base64.b64encode(body) if encoded else body


GOOD_RESPONSE = {
    "dungeonID": 0, "dungeonFloorNumber": 0, "gameMode": 1, "areaID": 1,
    "dungeonSeed": 123, "dungeonLayoutType": 3, "dungeonFloorType": 1,
    "dungeonFloorTotalCount": 3,
}


def _archive(tmp_path: Path, name: str) -> tuple[Path, Library]:
    root = tmp_path / name
    (root / "assets").mkdir(parents=True, exist_ok=True)
    return root, Library(root / "assets", root / "fx")


def _add(root: Path, lib: Library, dungeon: int, origin: str, *,
         runner: str = "Alyssiya", floor: int = 0) -> None:
    blob = f"local__dungeon-{dungeon}-floor-{floor}-layout-x"
    (root / "assets" / blob).write_text(LAYOUT, encoding="utf-8")
    response = dict(GOOD_RESPONSE, dungeonID=dungeon, dungeonFloorNumber=floor)
    lib.record_local_temple(dungeon, floor, response, blob, origin=origin)
    ghost = f"local__dungeon-{dungeon}-floor-{floor}-runinfo-a"
    (root / "assets" / ghost).write_bytes(recording())
    lib.record_local_run(dungeon, floor, ghost, {
        "userName": runner, "success": 2, "userID": 404409, "runID": 13562297,
        "platformUserID": "76561198000000042",   # the SteamID64: must not travel
    })


# ------------------------------------------------------- what may travel


def test_a_temple_we_invented_is_never_contributed(tmp_path: Path) -> None:
    """Its id collides with a real one, and it is worth nothing to anybody."""
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online")
    _add(root, lib, 391687, "client-offline")

    contribution = pool.build(root)
    assert [t["dungeonID"] for t in contribution.temples] == [747841]


def test_a_cdn_temple_travels(tmp_path: Path) -> None:
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 701203, "cdn")
    assert len(pool.build(root).temples) == 1


# ------------------------------------------------- what must never travel


def test_runner_names_travel_by_default(tmp_path: Path) -> None:
    """The name over a phantom is what makes it somebody's run.

    The game prints it above every phantom you run past, so it is already
    public to everyone who played. An archive that strips it is a temple full
    of "Explorer" -- the runs still play, they just stop being anybody's.
    """
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online", runner="Alyssiya")
    out = tmp_path / "contribution"
    report = pool.export(root, out)
    assert report["includesRunnerNames"] is True
    assert "Alyssiya" in (out / pool.MANIFEST).read_text("utf-8")


def test_names_can_be_left_out_deliberately(tmp_path: Path) -> None:
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online", runner="Alyssiya")
    out = tmp_path / "without-names"
    pool.export(root, out, include_names=False)
    manifest = json.loads((out / pool.MANIFEST).read_text("utf-8"))
    assert manifest["includesRunnerNames"] is False
    assert "Alyssiya" not in json.dumps(manifest)


def test_a_name_is_never_an_account(tmp_path: Path) -> None:
    """Names traveling does not mean anything behind them travels.

    This is the whole distinction: a display name is public in-game, while a
    SteamID64 is a profile link and an auth ticket is a credential.
    """
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online", runner="Alyssiya")
    out = tmp_path / "contribution"
    pool.export(root, out)

    everything = b"".join(f.read_bytes() for f in out.rglob("*") if f.is_file())
    assert b"Alyssiya" in everything
    assert b"76561198000000042" not in everything
    assert b"STEAM-TICKET" not in everything
    for blob in (out / pool.BLOBS).iterdir():
        assert not pool.carries_identity(blob.read_bytes())


def test_a_contribution_carries_no_credentials_or_profile(tmp_path: Path) -> None:
    """The state directory holds a live Steam auth ticket."""
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online")
    (root / "session").mkdir(parents=True, exist_ok=True)
    (root / "session" / "credentials.json").write_text(
        json.dumps({"verificationToken": "SECRET-TICKET",
                    "playerId": "76561198000000042"}), encoding="utf-8")
    (root / "state" / "profiles").mkdir(parents=True, exist_ok=True)
    (root / "state" / "profiles" / "76561198000000042.json").write_text(
        json.dumps({"reputation": 5763}), encoding="utf-8")

    out = tmp_path / "contribution"
    pool.export(root, out)
    everything = "".join(
        p.read_text("utf-8", errors="replace") for p in out.rglob("*") if p.is_file()
    )
    assert "SECRET-TICKET" not in everything
    assert "76561198000000042" not in everything
    assert "reputation" not in everything


# ------------------------------------------------------ checked at the door


def _contribution(tmp_path: Path, mutate=None) -> Path:
    root, lib = _archive(tmp_path, "sender")
    _add(root, lib, 747841, "client-online")
    out = tmp_path / "incoming"
    pool.export(root, out)
    if mutate:
        mutate(out)
    return out


def test_a_blob_that_does_not_match_its_hash_is_refused(tmp_path: Path) -> None:
    def tamper(out: Path) -> None:
        manifest = json.loads((out / pool.MANIFEST).read_text("utf-8"))
        sha = manifest["temples"][0]["layout"]["sha256"]
        (out / pool.BLOBS / sha).write_text("something else", encoding="utf-8")

    receiver, _ = _archive(tmp_path, "receiver")
    result = pool.ingest(receiver, _contribution(tmp_path, tamper))
    assert result.accepted == 0
    assert "layout does not match its hash" in result.reasons


def test_a_layout_that_will_not_decode_is_refused(tmp_path: Path) -> None:
    def tamper(out: Path) -> None:
        manifest = json.loads((out / pool.MANIFEST).read_text("utf-8"))
        junk = b"not a temple at all"
        sha = hashlib.sha256(junk).hexdigest()
        (out / pool.BLOBS / sha).write_bytes(junk)
        manifest["temples"][0]["layout"]["sha256"] = sha
        (out / pool.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    receiver, _ = _archive(tmp_path, "receiver")
    result = pool.ingest(receiver, _contribution(tmp_path, tamper))
    assert result.accepted == 0
    assert "layout will not decode" in result.reasons


def test_a_layout_with_no_shape_is_refused(tmp_path: Path) -> None:
    """Exactly the corruption that crashed the game twice while building this."""
    def tamper(out: Path) -> None:
        manifest = json.loads((out / pool.MANIFEST).read_text("utf-8"))
        manifest["temples"][0]["response"].pop("dungeonLayoutType", None)
        (out / pool.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    receiver, _ = _archive(tmp_path, "receiver")
    result = pool.ingest(receiver, _contribution(tmp_path, tamper))
    assert result.accepted == 0
    assert "layout without a shape" in result.reasons


def test_a_temple_claiming_to_be_offline_is_refused(tmp_path: Path) -> None:
    def tamper(out: Path) -> None:
        manifest = json.loads((out / pool.MANIFEST).read_text("utf-8"))
        manifest["temples"][0]["origin"] = "client-offline"
        (out / pool.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    receiver, _ = _archive(tmp_path, "receiver")
    result = pool.ingest(receiver, _contribution(tmp_path, tamper))
    assert result.accepted == 0
    assert "not from the live service" in result.reasons


def test_an_unreadable_manifest_is_refused(tmp_path: Path) -> None:
    receiver, _ = _archive(tmp_path, "receiver")
    incoming = tmp_path / "junk"
    incoming.mkdir()
    (incoming / pool.MANIFEST).write_text("{ not json", encoding="utf-8")
    result = pool.ingest(receiver, incoming)
    assert result.accepted == 0
    assert "unreadable manifest" in result.reasons


# ------------------------------------------------------------ round trip


def test_what_arrives_is_playable(tmp_path: Path) -> None:
    receiver, _ = _archive(tmp_path, "receiver")
    result = pool.ingest(receiver, _contribution(tmp_path))
    assert result.accepted == 1
    assert result.phantoms == 1

    lib = Library(receiver / "assets", receiver / "fx")
    temple = lib.temples[(747841, 0)]
    assert temple.layout
    assert temple.response["dungeonLayoutType"] == 3
    assert len(temple.ghosts) == 1


def test_what_arrives_can_be_passed_on(tmp_path: Path) -> None:
    """A pooled temple is still real content, so it keeps traveling."""
    receiver, _ = _archive(tmp_path, "receiver")
    pool.ingest(receiver, _contribution(tmp_path))
    onward = pool.build(receiver)
    assert [t["dungeonID"] for t in onward.temples] == [747841]


def test_only_what_is_missing_needs_sending(tmp_path: Path) -> None:
    """The reason for hashing: two people who ran the same temple send it once."""
    sender, slib = _archive(tmp_path, "sender")
    _add(sender, slib, 747841, "client-online")
    _add(sender, slib, 747842, "client-online")

    receiver, rlib = _archive(tmp_path, "receiver")
    _add(receiver, rlib, 747841, "client-online")

    manifest = pool.build(sender).manifest(names=False)
    missing = pool.wanted(receiver, manifest)
    offered = {t["layout"]["sha256"] for t in manifest["temples"]}
    assert offered - set(missing), "the shared temple was not recognized"


def test_a_run_is_recorded_once_per_floor(tmp_path: Path) -> None:
    """Counting recordings as ghosts roughly doubles the truth.

    Measured across 866 consecutive floor pairs in real captures: 93% of the
    players on a floor were already on the one before it, carrying on the same
    journey -- while 0% of runIDs repeated, because each floor is its own
    recording. Both facts matter, so the census reports both.
    """
    root, lib = _archive(tmp_path, "player")
    for floor in range(3):
        blob = f"local__dungeon-747841-floor-{floor}-layout-x"
        (root / "assets" / blob).write_text(LAYOUT, encoding="utf-8")
        lib.record_local_temple(
            747841, floor,
            dict(GOOD_RESPONSE, dungeonID=747841, dungeonFloorNumber=floor),
            blob, origin="client-online",
        )
        # The same two people, carrying on down the temple.
        for who in (4001, 4002):
            ghost = f"local__dungeon-747841-floor-{floor}-runinfo-{who}"
            (root / "assets" / ghost).write_bytes(recording())
            lib.record_local_run(747841, floor, ghost,
                                 {"userID": who, "userName": f"p{who}"})

    census = Library(root / "assets", root / "fx").census()
    assert census["recordings"] == 6, "one per player per floor"
    assert census["runs"] == 2, "two journeys, not six"
    assert census["people"] == 2


def test_the_census_survives_phantoms_with_no_owner(tmp_path: Path) -> None:
    """Locally recorded runs may carry no userID; they still count as content."""
    root, lib = _archive(tmp_path, "player")
    _add(root, lib, 747841, "client-online")
    census = Library(root / "assets", root / "fx").census()
    assert census["recordings"] == 1
    assert census["runs"] >= 1, "an unowned recording is still a run"


# ------------------------------------------------- whose run it was


def test_a_contribution_carries_no_steam_id_or_auth_ticket(tmp_path: Path) -> None:
    """The names are cosmetic. What is underneath them is not.

    A recording is stored and served back exactly as the client posted it, so it
    still holds the SteamID64 and the Steam auth ticket that submission needed.
    Those travel with the blob unless something takes them out, and the names
    toggle never guarded them -- it only ever governed the display name.
    """
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online")
    out = tmp_path / "contribution"
    pool.export(root, out, include_names=True)

    everything = b"".join(f.read_bytes() for f in out.rglob("*") if f.is_file())
    assert b"76561198000000042" not in base64.b64decode(
        next((out / pool.BLOBS).iterdir()).read_bytes())
    for blob in (out / pool.BLOBS).iterdir():
        assert not pool.carries_identity(blob.read_bytes())
    assert b"STEAM-TICKET" not in everything


def test_scrubbing_keeps_the_shape_the_client_parses(tmp_path: Path) -> None:
    """Blank the values, keep the keys -- the client sees the struct it knows."""
    cleaned = pool.scrub_recording(recording())
    record = json.loads(base64.b64decode(cleaned))
    assert set(record) == set(json.loads(base64.b64decode(recording())))
    assert record["playerId"] == ""
    assert record["verificationToken"] == ""
    assert record["numRecordedFrames"] == 4210, "the run itself is untouched"
    assert record["userId"] == 62842, "the in-game id is not a profile link"


def test_scrubbing_is_idempotent(tmp_path: Path) -> None:
    """A recording that has already traveled must hash the same next time.

    Otherwise every hop through a pool would mint a new hash for identical
    content, and the deduplication that makes pooling cheap would stop working.
    """
    once = pool.scrub_recording(recording())
    assert pool.scrub_recording(once) == once


def test_a_plain_recording_is_scrubbed_too(tmp_path: Path) -> None:
    """Not every recording arrives base64-wrapped; the envelope is preserved."""
    cleaned = pool.scrub_recording(recording(encoded=False))
    assert json.loads(cleaned)["playerId"] == ""


def test_an_unreadable_recording_is_held_back(tmp_path: Path) -> None:
    """Cannot read it, cannot prove it is clean, so it does not travel."""
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online")
    ghost = root / "assets" / "local__dungeon-747841-floor-0-runinfo-a"
    ghost.write_bytes(b"\x00\x01 opaque")

    report = pool.export(root, tmp_path / "contribution")
    assert report["unreadable"] == 1
    manifest = json.loads((tmp_path / "contribution" / pool.MANIFEST).read_text("utf-8"))
    assert manifest["temples"][0]["phantoms"] == []


def test_a_recording_that_still_names_its_runner_is_refused(tmp_path: Path) -> None:
    """A sender running an older client must not be able to hand us one."""
    def tamper(out: Path) -> None:
        manifest = json.loads((out / pool.MANIFEST).read_text("utf-8"))
        dirty = recording("76561198000000666")
        sha = hashlib.sha256(dirty).hexdigest()
        (out / pool.BLOBS / sha).write_bytes(dirty)
        manifest["temples"][0]["phantoms"][0]["sha256"] = sha
        (out / pool.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    receiver, _ = _archive(tmp_path, "receiver")
    result = pool.ingest(receiver, _contribution(tmp_path, tamper))
    assert result.phantoms == 0
    assert "recording still names its runner" in result.reasons


def test_the_game_s_own_ids_travel_but_the_platform_s_does_not(tmp_path: Path) -> None:
    """A phantom needs to be the same person on the next floor down.

    `userID` and `runID` are the game's numbering -- they carry no profile and
    lead nowhere outside the game, but they are what lets a client recognize
    the runner who got you through the last floor, and what gives each spawned
    actor a name of its own. `platformUserID` is a SteamID64 and never travels.
    """
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online", runner="Alyssiya")
    out = tmp_path / "contribution"
    pool.export(root, out)

    manifest = json.loads((out / pool.MANIFEST).read_text("utf-8"))
    phantom = manifest["temples"][0]["phantoms"][0]
    assert phantom["userID"] == 404409
    assert phantom["runID"] == 13562297
    assert phantom["userName"] == "Alyssiya"
    assert "platformUserID" not in phantom

    everything = "".join(f.read_text("utf-8", errors="replace")
                         for f in out.rglob("*") if f.is_file())
    assert "76561198000000042" not in everything


def test_a_pooled_phantom_keeps_its_identity_through_ingest(tmp_path: Path) -> None:
    receiver, _ = _archive(tmp_path, "receiver")
    pool.ingest(receiver, _contribution(tmp_path))
    temple = Library(receiver / "assets", receiver / "fx").temples[(747841, 0)]
    served = temple.ghost_runs("http://127.0.0.1:1")[0]
    assert served["userID"] == 404409, "the real id survives the round trip"
    assert served["userName"] == "Alyssiya"


# ------------------------------------------- what could not be finished


def test_reports_travel_with_the_temples_they_are_about(tmp_path: Path) -> None:
    """A broken temple and the knowledge that it is broken are no use apart.

    Reporting used to produce a separate file nobody remembered to send. A
    recipient who gets the temple without the finding spends a run discovering
    it for themselves, which is exactly the waste the report exists to stop.
    """
    from phantom_offline.blacklist import Blacklist

    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online")
    Blacklist(root / "state" / "blacklist.json").report_archived(
        747841, 0, reason="missing-key", note="exit door never opened")

    out = tmp_path / "contribution"
    report = pool.export(root, out)
    assert report["reports"] == 1

    manifest = json.loads((out / pool.MANIFEST).read_text("utf-8"))
    carried = manifest["reports"][0]
    assert carried["reason"] == "missing-key"
    assert carried["count"] == 1
    assert "reporters" not in carried, "who reported it is nobody's business"


def test_an_incoming_report_does_not_withhold_on_one_persons_word(
    tmp_path: Path,
) -> None:
    """The recipient trusts a stranger's finding exactly as far as they should."""
    from phantom_offline.blacklist import Blacklist

    sender, lib = _archive(tmp_path, "sender")
    _add(sender, lib, 747841, "client-online")
    Blacklist(sender / "state" / "blacklist.json").report_archived(
        747841, 0, reason="missing-key")

    out = tmp_path / "contribution"
    pool.export(sender, out)

    receiver, _ = _archive(tmp_path, "receiver")
    result = pool.ingest(receiver, out)
    assert result.accepted == 1
    assert result.reports == 1

    black = Blacklist(receiver / "state" / "blacklist.json")
    assert black.blocks_temple(747841, 0) is False, "one stranger is not enough"

    # The temple still arrived: preservation first, with the warning attached.
    kept = Library(receiver / "assets", receiver / "fx")
    assert kept.lookup(747841, 0) is not None


def test_two_contributions_agreeing_withholds_it(tmp_path: Path) -> None:
    from phantom_offline.blacklist import Blacklist

    receiver, _ = _archive(tmp_path, "receiver")
    for who in ("first", "second"):
        sender, lib = _archive(tmp_path, who)
        _add(sender, lib, 747841, "client-online")
        Blacklist(sender / "state" / "blacklist.json").report_archived(
            747841, 0, reason="missing-key")
        out = tmp_path / f"contribution-{who}"
        pool.export(sender, out)
        pool.ingest(receiver, out)

    black = Blacklist(receiver / "state" / "blacklist.json")
    assert black.blocks_temple(747841, 0) is True


def test_a_contribution_with_no_reports_is_fine(tmp_path: Path) -> None:
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online")
    out = tmp_path / "contribution"
    assert pool.export(root, out)["reports"] == 0

    receiver, _ = _archive(tmp_path, "receiver")
    assert pool.ingest(receiver, out).reports == 0


def test_the_same_contribution_imported_twice_is_still_one_finding(
    tmp_path: Path,
) -> None:
    """Otherwise the threshold is met by importing the same folder again."""
    from phantom_offline.blacklist import Blacklist

    sender, lib = _archive(tmp_path, "sender")
    _add(sender, lib, 747841, "client-online")
    Blacklist(sender / "state" / "blacklist.json").report_archived(
        747841, 0, reason="missing-key")
    out = tmp_path / "contribution"
    pool.export(sender, out)

    receiver, _ = _archive(tmp_path, "receiver")
    for _ in range(3):
        pool.ingest(receiver, out)

    black = Blacklist(receiver / "state" / "blacklist.json")
    assert black.get(pool_report_id(747841, 0)).count == 1
    assert black.blocks_temple(747841, 0) is False


def pool_report_id(dungeon: int, floor: int) -> str:
    from phantom_offline.blacklist import archived_id

    return archived_id(dungeon, floor)


def test_a_name_that_is_a_steam_id_does_not_travel(tmp_path: Path) -> None:
    """Found by grepping a real running server, not by reading the code.

    Names travel because the game already shows them above every phantom. Some
    players are called by their SteamID64, and a name that is an account is an
    account whatever field it arrives in -- so it fails the promise the format
    makes even though nothing was leaking from where identities were expected.
    """
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online", runner="76561198000000555")
    out = tmp_path / "contribution"
    pool.export(root, out)

    manifest = (out / pool.MANIFEST).read_text("utf-8")
    assert "76561198000000555" not in manifest
    assert '"userName": "Explorer"' in manifest


def test_an_ordinary_name_is_untouched(tmp_path: Path) -> None:
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online", runner="Mr_merv")
    out = tmp_path / "contribution"
    pool.export(root, out)
    assert "Mr_merv" in (out / pool.MANIFEST).read_text("utf-8")


def test_a_numeric_name_that_is_not_an_account_is_kept(tmp_path: Path) -> None:
    """Plenty of real players are called things like 2760523185."""
    root, lib = _archive(tmp_path, "alice")
    _add(root, lib, 747841, "client-online", runner="2760523185")
    out = tmp_path / "contribution"
    pool.export(root, out)
    assert "2760523185" in (out / pool.MANIFEST).read_text("utf-8")
