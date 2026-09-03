"""Entry point: `python -m phantom_offline`.

    serve    run the offline server (default)
    launch   configure the client and start the game against it
    status   show what has been captured so far
    patch    point the game's backend URLs at the local server
    unpatch  restore the original executable
    restore  undo the Engine.ini changes
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import client_config, patch, server

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Where the archive lives. A full preservation run is hundreds of gigabytes, so
# it usually belongs on a different drive from the code. Resolution order:
#   1. --state on the command line
#   2. $PHANTOM_OFFLINE_STATE
#   3. the path written in .archive_path next to the project
#   4. ./run
ARCHIVE_POINTER = PROJECT_ROOT / ".archive_path"


def _default_state() -> Path:
    from os import environ

    override = environ.get("PHANTOM_OFFLINE_STATE")
    if override:
        return Path(override)
    if ARCHIVE_POINTER.exists():
        try:
            recorded = ARCHIVE_POINTER.read_text("utf-8").strip()
            if recorded:
                return Path(recorded)
        except OSError:
            pass

    # A packaged build unpacks itself into a temporary directory that is
    # deleted on exit, so anything written beside the program is gone the
    # moment the player closes it -- every temple, phantom and profile from
    # that session. Frozen builds keep their state somewhere that survives.
    if getattr(sys, "frozen", False):
        home = _app_home()
        chosen = _chosen_state(home)
        return chosen or home

    return PROJECT_ROOT / "run"


def _app_home() -> Path:
    """Where a packaged build keeps its own things.

    The folder was called PhantomAbyssOffline before this was named, and
    anybody who ran an early build has an archive sitting in it. A rename that
    silently abandoned somebody's temples and recordings would be a poor
    introduction, so the old folder is still used when it is the one that
    exists.
    """
    from os import environ

    base = environ.get("LOCALAPPDATA") or environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home()
    home = root / "CairnKeeper"
    if not home.exists():
        earlier = root / "PhantomAbyssOffline"
        if earlier.is_dir():
            return earlier
    return home


def _chosen_state(home: Path) -> Path | None:
    """An archive the player has pointed us at, if they have.

    People who already have an archive should not have to move six gigabytes
    to use the packaged client, and people who want theirs on another drive
    should be able to say so.
    """
    try:
        recorded = (home / "archive-location.txt").read_text("utf-8").strip()
    except OSError:
        return None
    return Path(recorded) if recorded else None


def remember_state(path: Path) -> None:
    home = _app_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "archive-location.txt").write_text(str(path), encoding="utf-8")


DEFAULT_STATE = _default_state()


def _writable(state_dir: Path) -> list[str]:
    """Whether the archive can be written to, in words an operator can act on.

    Hosting this in a container is the case that gets it wrong: the
    directory belongs to whoever created it and the container runs as
    somebody else, so the first write fails. A traceback about
    `/archive/capture` is a poor way to find that out.

    Returns the lines to print, empty when all is well.
    """
    import os

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return []
    except OSError as exc:
        who = ""
        getuid = getattr(os, "getuid", None)
        if getuid is not None:
            who = f" (running as uid {getuid()})"
        return [
            f"cannot write to {state_dir}{who}: {exc.strerror or exc}",
            "",
            "The archive has to belong to whoever runs the server.",
            "In Docker it runs as uid 1000 unless told otherwise. If yours is",
            "different, record it once and bring the stack back up:",
            "    echo PHANTOM_UID=$(id -u) > .env",
            "    echo PHANTOM_GID=$(id -g) >> .env",
            "    docker compose up -d",
        ]


def cmd_serve(args: argparse.Namespace) -> int:
    trouble = _writable(args.state)
    if trouble:
        print(f"error: {trouble[0]}", file=sys.stderr)
        for line in trouble[1:]:
            print(line, file=sys.stderr)
        return 1

    for port in (args.port, args.proxy_port):
        if not server.port_is_free(port):
            print(f"error: port {port} is already in use", file=sys.stderr)
            return 1

    mode = server.MODE_CAPTURE if args.capture else server.MODE_OFFLINE
    direct, proxy, recorder = server.build(
        state_dir=args.state,
        direct_port=args.port,
        proxy_port=args.proxy_port,
        verbose=args.verbose,
        mode=mode,
        host=getattr(args, "host", "127.0.0.1"),
    )
    # Carry across whatever the last live session banked. Profiles are cached
    # once a server is running, so this has to happen before it starts serving
    # or the update is invisible until the next restart.
    if mode == server.MODE_OFFLINE:
        try:
            carried = direct.backend.sync_from_capture()
        except Exception as exc:  # noqa: BLE001 - never block startup
            print(f"  progression sync  : skipped ({exc})")
            carried = []
        if carried:
            print(f"  progression sync  : carried over for {len(carried)} player(s)")

    print("Phantom Abyss offline server")
    print(f"  mode              : {mode}")
    if mode == server.MODE_CAPTURE:
        print("     -> forwarding to the REAL wiby.net servers and archiving")
        print(f"     -> fixtures: {args.state / 'fixtures'}")
    print(f"  direct (ST_Local) : http://127.0.0.1:{args.port}")
    print(f"  proxy             : 127.0.0.1:{args.proxy_port}")
    print(f"  state             : {args.state}")
    print(f"  capture           : {recorder.transcript}")
    print("\nCtrl+C to stop.\n")
    try:
        server.serve_forever([direct, proxy])
    except KeyboardInterrupt:
        print(f"\nstopped after {recorder.count} request(s)")
    return 0


def cmd_pool(args: argparse.Namespace) -> int:
    """Share what you kept, and take in what others kept."""
    from . import pool

    if args.action == "status":
        counts = pool.summary(args.state)
        print(f"temples you could contribute : {counts['temples']:,}")
        print(f"phantoms in them             : {counts['phantoms']:,}")
        print(f"total size                   : {counts['bytes'] / 1e9:.2f} GB")
        print()
        print("Only temples the real service minted. Ones generated offline are")
        print("yours alone -- anyone can make their own, and their ids would")
        print("collide with real temples.")
        return 0

    if args.action == "export":
        if not args.out:
            print("export needs --out <folder>", file=sys.stderr)
            return 2
        have = set()
        if args.against:
            try:
                manifest = json.loads(
                    (Path(args.against) / pool.MANIFEST).read_text("utf-8")
                )
                have = set(pool.wanted(args.state, manifest))
            except (OSError, ValueError):
                print(f"could not read a manifest at {args.against}", file=sys.stderr)
                return 1
        report = pool.export(args.state, args.out,
                             include_names=not args.no_names, have=have)
        print(f"temples : {report['temples']:,}")
        print(f"files   : {report['blobs']:,}  ({report['bytes'] / 1e6:.1f} MB)")
        if report["skipped"]:
            print(f"skipped : {report['skipped']:,} the recipient already has")
        print(f"written to {args.out}")
        print()
        if report["includesRunnerNames"]:
            print("Runners' display names are included, the same ones the game")
            print("shows above each phantom. No Steam IDs, no friend codes, no")
            print("auth tickets, no accounts, no profile.")
        else:
            print("Runners' display names are left out, so every phantom will")
            print("appear as \"Explorer\". Nothing else changes.")
        return 0

    if args.action == "import":
        if not args.folder:
            print("import needs --folder <contribution>", file=sys.stderr)
            return 2
        result = pool.ingest(args.state, args.folder)
        print(f"accepted : {result.accepted:,} temple(s)")
        print(f"phantoms : {result.phantoms:,}")
        if result.rejected:
            print(f"rejected : {result.rejected:,}")
            for why, n in sorted(result.reasons.items(), key=lambda x: -x[1]):
                print(f"    {n:>5}  {why}")
        return 0

    print(f"unknown action {args.action!r}", file=sys.stderr)
    return 2


def cmd_window(args: argparse.Namespace) -> int:
    """Open the window. This is what the packaged build starts with."""
    from .gui import main as gui_main

    return gui_main(args.state)


def cmd_fidelity(args: argparse.Namespace) -> int:
    """Compare our answers against the real server's, from captured traffic."""
    from . import fidelity

    report = fidelity.compare(args.state, limit=args.limit)
    print(report.summary())
    if not report.checked:
        print("Capture a session first: play --listen")
        return 0

    print()
    for div in sorted(report.divergences, key=lambda d: -d.count):
        print(f"  {div.count:>6}x  {div.endpoint}.{div.field_path}")
        print(f"           {div.kind}: real={div.real or '-'}  ours={div.ours or '-'}")

    path = fidelity.save(args.state, report)
    print()
    print(f"Written to {path}")
    print("It lists fields and types only. No accounts, no names, no traffic --")
    print("open it and read it before sending it anywhere.")
    return 0


def cmd_bundles(args: argparse.Namespace) -> int:
    """Show shared content, and let the player choose what to accept."""
    from . import bundles as bundle_store

    if args.enable or args.disable:
        key = args.enable or args.disable
        if not bundle_store.set_enabled(args.state, key, bool(args.enable)):
            print(f"no bundle called {key!r}", file=sys.stderr)
            print()
            print("Installed:")
            for line in bundle_store.describe(args.state):
                print(line)
            return 1
        print(f"{key}: {'on' if args.enable else 'off'}")
        print("Restart the server for this to take effect.")
        return 0

    print("Shared content")
    print()
    for line in bundle_store.describe(args.state):
        print(line)
    print()
    print("Your own captures are always used. Bundles are extra temples and")
    print("phantoms other people gathered, and start switched off.")
    return 0


def cmd_play(args: argparse.Namespace) -> int:
    """Patch, serve, launch, and put everything back afterwards."""
    from .supervisor import Supervisor, describe

    sup = Supervisor(
        args.state, port=args.port, proxy_port=args.proxy_port, verbose=args.verbose
    )
    found = sup.preflight()
    for line in describe(found):
        print(f"  {line}")
    print()

    if args.check:
        if found.ok:
            print("Ready to play.")
            return 0
        for problem in found.problems:
            print(f"  {problem}")
        return 1

    mode = "listen" if args.listen else "host"
    print(f"Starting in {mode} mode.")
    if mode == "listen":
        print("  -> talking to the real servers, keeping everything you touch")
    else:
        print("  -> answered locally, from everything you have kept")
    print()
    return sup.run(mode, launch=not args.no_game, extra_args=args.game_args)


def cmd_launch(args: argparse.Namespace) -> int:
    return client_config.launch(
        proxy_port=args.proxy_port,
        dry_run=args.dry_run,
        extra_args=args.game_args,
        via_steam=args.via_steam,
        ca_cert=args.state / "certs" / "ca.crt",
    )


def _resolve_exe() -> Path | None:
    game = client_config.find_game()
    if game is None:
        print("error: could not find the Phantom Abyss install", file=sys.stderr)
        return None
    exe = game / client_config.SHIPPING_EXE
    if not exe.exists():
        print(f"error: shipping executable not found at {exe}", file=sys.stderr)
        return None
    return exe


def cmd_patch(args: argparse.Namespace) -> int:
    exe = _resolve_exe()
    if exe is None:
        return 1
    backup_dir = args.state / "backup"

    try:
        count, backup = patch.apply(
            exe, backup_dir=backup_dir, port=args.port, dry_run=args.dry_run
        )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if count == 0:
        print("no backend URLs found -- already patched?")
        return 0
    if args.dry_run:
        print(f"would rewrite {count} URL site(s) in {exe.name} (dry run)")
        return 0

    print(f"rewrote {count} URL site(s) in {exe.name}")
    print(f"  -> http://127.0.0.1:{args.port}")
    print(f"backup: {backup}")
    print("\nThe game now speaks plain HTTP to the local server; no proxy or")
    print("certificate is involved. Undo with `unpatch`, or by verifying the")
    print("game files through Steam.")
    return 0


def cmd_unpatch(args: argparse.Namespace) -> int:
    exe = _resolve_exe()
    if exe is None:
        return 1
    if patch.restore(exe, backup_dir=args.state / "backup"):
        print(f"restored {exe.name} from backup")
        return 0
    print(
        "no backup found. Verify the game files through Steam to restore the "
        "original executable.",
        file=sys.stderr,
    )
    return 1


def cmd_archive(args: argparse.Namespace) -> int:
    """Download every blob referenced by already-captured fixtures."""
    import json

    from .assets import AssetArchiver

    archiver = AssetArchiver(args.state / "assets", workers=args.workers)
    queued = 0
    for path in sorted((args.state / "fixtures").glob("*.jsonl")):
        for line in path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            queued += archiver.harvest(entry.get("response_body"))

    if not queued:
        print("nothing new to archive")
        return 0

    print(f"downloading {queued} blob(s) -> {args.state / 'assets'}")
    archiver.drain()
    print(f"\nfetched {archiver.fetched}, failed {archiver.failed}")
    if archiver.failed:
        print("(failures are recorded in assets/index.json)")
    return 0


def cmd_harvest(args: argparse.Namespace) -> int:
    """Archive fresh temples straight from the live server."""
    from .harvest import Harvester, archive_size, complete_count

    harvester = Harvester(
        args.state,
        delay=args.delay,
        mode=args.mode,
        difficulty=args.difficulty,
        curse_level=args.curse_level,
        area=args.area,
    )
    before_files, before_mb = archive_size(args.state)
    before_complete = complete_count(args.state)

    print(f"game mode        : {args.mode}")
    if args.area:
        print(f"area             : {args.area}")
    print(f"temples to fetch : {args.count}")
    print(f"delay            : {args.delay}s between requests")
    print(f"archive before   : {before_complete} complete temple(s), "
          f"{before_files} blob(s), {before_mb:.1f} MB")
    print(
        "\nEach temple mints one route on the live server, exactly as starting a\n"
        "run does. No run submissions are made, so nothing is claimed that did\n"
        "not happen.\n"
    )

    if args.dry_run:
        print("(dry run -- no requests sent)")
        return 0

    try:
        result = harvester.run(count=args.count)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    after_files, after_mb = archive_size(args.state)
    after_complete = complete_count(args.state)

    print(
        f"\ntemples {result.temples}, floors {result.floors}, "
        f"blobs {result.blobs}, failures {result.failures}"
    )
    print(
        f"archive now      : {after_complete} complete temple(s) "
        f"(+{after_complete - before_complete}), "
        f"{after_files} blob(s) (+{after_files - before_files}), "
        f"{after_mb:.1f} MB (+{after_mb - before_mb:.1f})"
    )
    if result.stopped_early:
        print(f"\nstopped early: {result.stopped_early}")
        print("If the token expired, re-capture with `serve --capture` and rerun.")
        return 1
    return 0


def cmd_resync(args: argparse.Namespace) -> int:
    """Refresh a profile from the newest captured login for that account.

    A profile is seeded once and then owned by the offline server. If capturing
    continued afterwards, the seed can be older than the archive -- so a
    player's latest online progress would be silently missing.
    """
    from .library import Library
    from .profile import PROFILE_FIELDS, Profile

    # Captures and play often live in different state directories: the archive
    # holds the capture sessions, while a player's offline profile lives
    # wherever they run the server. --from-state points at the former.
    source = getattr(args, "from_state", None) or args.state
    library = Library(source / "assets", source / "fixtures")
    latest = library.latest_login(args.user_id)
    if latest is None:
        known = ", ".join(str(k) for k in sorted(library.user_responses))
        print(
            f"no captured login for userID {args.user_id}."
            + (f" Known: {known}" if known else ""),
            file=sys.stderr,
        )
        return 1

    path = args.state / "state" / "profiles" / f"{args.player}.json"
    profile = Profile(path)
    before = profile.snapshot()

    changed = []
    for field in PROFILE_FIELDS:
        value = latest.get(field)
        if value is None:
            continue
        if json.dumps(before.get(field), sort_keys=True) != json.dumps(
            value, sort_keys=True
        ):
            changed.append(field)
        profile.data[field] = value
    profile.data["userID"] = latest.get("userID")

    # A login is only current until the next run ends. Apply anything newer the
    # live server stated, or the profile lands a session behind.
    fresher = library.latest_progression(args.user_id)
    for field in ("currency", "reputation"):
        value = fresher.get(field)
        if value is None:
            continue
        if json.dumps(profile.data.get(field), sort_keys=True) != json.dumps(
            value, sort_keys=True
        ):
            if field not in changed:
                changed.append(field)
            profile.data[field] = value
    stamps = fresher.get("_seenAt") or {}
    if stamps:
        newest = max(stamps.values(), default="")
        print(f"newest live statement of standing: {newest[:19] or 'unknown'}")
    profile.save()

    if not changed:
        print("profile already matches the newest capture")
        return 0
    print(f"resynced {path.name} from the newest capture for userID {args.user_id}")
    for field in changed:
        print(f"  updated: {field}")
    return 0


def cmd_shares(args: argparse.Namespace) -> int:
    """Look up share codes and report what each player has shared."""
    from .shares import ShareLookup, decode_share, parse_codes, safe

    raw = args.codes_file.read_text("utf-8") if args.codes_file else " ".join(args.codes)
    codes = parse_codes(raw, length=None if args.any_length else 8)
    if not codes:
        print("no share-code-shaped tokens found", file=sys.stderr)
        return 1

    print(f"{len(codes)} share code(s) to look up, {args.delay}s apart\n")
    if args.dry_run:
        for code in codes[:30]:
            print(f"  {code}")
        if len(codes) > 30:
            print(f"  ... and {len(codes) - 30} more")
        return 0

    lookup = ShareLookup(args.state, delay=args.delay)
    try:
        results = lookup.run(codes)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    found = [r for r in results if r.exists]
    with_routes = [r for r in found if r.routes]
    print(
        f"\n{len(results)} looked up, {len(found)} real account(s), "
        f"{len(with_routes)} with shared routes"
    )

    for result in with_routes:
        print(f"\n{result.username} ({result.code}):")
        for entry in result.routes:
            decoded = decode_share(entry)
            print(f"   {json.dumps(decoded) if decoded else '(undecodable)'}")

    if found and not with_routes:
        print("\nNobody had shared a route. The lookup works; there is just")
        print("nothing published behind these codes.")
    return 0


def cmd_shareharvest(args: argparse.Namespace) -> int:
    """Archive the temples behind routes other players have shared."""
    from .shares import ShareHarvester, routes_from_fixtures, shared_dungeons

    routes = routes_from_fixtures(args.state / "fixtures")
    if not routes:
        print(
            "no shared routes recorded. Run `shares --codes-file ...` first.",
            file=sys.stderr,
        )
        return 1

    dungeons = {
        d["dungeonID"]: d for r in routes for d in shared_dungeons(r)
    }
    print(f"shared routes    : {len(routes)}")
    print(f"distinct temples : {len(dungeons)}")
    ghosts = sum(d.get("numGhosts") or 0 for d in dungeons.values())
    print(f"phantoms claimed : {ghosts:,}")
    if args.limit:
        print(f"limit            : {args.limit} route(s)")
    if args.dry_run:
        print("\n(dry run)")
        return 0

    harvester = ShareHarvester(args.state, delay=args.delay)
    try:
        stats = harvester.run(routes, limit=args.limit)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"\nroutes {stats['routes']}, floors {stats['floors']}, "
        f"failed {stats['failed']}, phantoms seen {stats['ghosts']:,}"
    )
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    """Force every fresh run on this server to a chosen temple."""
    from .backend import MODE_NAMES, OfflineBackend
    from .library import Library

    library = Library(args.state / "assets", args.state / "fixtures")
    backend = OfflineBackend(args.state / "state", archive=library)

    if args.clear:
        backend.pin_temple(None)
        print("pin cleared -- fresh runs rotate through the archive again")
        return 0

    if args.dungeon is None:
        current = backend.state.get("pinnedDungeon")
        print(f"pinned temple: {current or 'none'}")
        print()
        print("complete temples available to pin:")
        for mode, ids in sorted(
            library.by_mode().items(), key=lambda x: (x[0] is None, x[0])
        ):
            name = MODE_NAMES.get(mode, "unknown")
            print(f"  {name}:")
            for line in _wrap([str(i) for i in ids], width=8):
                print(f"      {line}")
        return 0

    if args.dungeon not in library.complete_dungeons():
        print(
            f"dungeon {args.dungeon} is not fully archived, so a run would "
            "dead-end on a floor we cannot serve.",
            file=sys.stderr,
        )
        return 1

    backend.pin_temple(args.dungeon)
    floors = library.floors_expected(args.dungeon)
    print(f"pinned dungeon {args.dungeon} ({floors} floor(s))")
    print("Every player starting a fresh run now gets this temple.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Report, review and share temples that cannot be finished."""
    import json as _json

    from .backend import OfflineBackend
    from .blacklist import (
        REASONS,
        STATUS_CLEARED,
        STATUS_CONFIRMED,
        Fingerprint,
        summarize,
    )
    from .library import Library

    library = Library(args.state / "assets", args.state / "fixtures")
    backend = OfflineBackend(args.state / "state", archive=library)
    blacklist = backend.blacklist

    action = args.action or "list"

    if action == "reasons":
        print("reason codes:")
        for code, meaning in REASONS.items():
            print(f"  {code:<18} {meaning}")
        return 0

    if action == "list":
        reports = blacklist.reports()
        print(summarize(reports))
        if not reports:
            print("\nNothing has been reported as unplayable.")
            print("After hitting a temple you cannot finish, run:")
            print("  python -m phantom_offline report last --reason missing-key")
            return 0
        print()
        for report in sorted(reports, key=lambda r: r.lastSeen, reverse=True):
            mark = "BLOCKED" if report.blocks() else "       "
            src = report.source
            print(f"  {mark} [{report.status}/{src} x{report.count}] {report.describe()}")
            if report.note:
                print(f"          note: {report.note}")
        return 0

    if action == "export":
        payload = blacklist.export()
        text = _json.dumps(payload, indent=2)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
            print(f"wrote {len(payload['reports'])} report(s) to {args.out}")
            print("This file carries no player ids and is safe to share.")
        else:
            print(text)
        return 0

    if action == "import":
        if not args.file:
            print("import needs --file <exported.json>")
            return 2
        payload = _json.loads(args.file.read_text("utf-8"))
        added, merged = blacklist.import_reports(payload)
        print(f"imported: {added} new, {merged} merged into existing")
        print(summarize(blacklist.reports()))
        return 0

    if action in ("confirm", "clear", "remove"):
        if not args.id:
            print(f"{action} needs --id <report id>  (see: report list)")
            return 2
        if action == "remove":
            ok = blacklist.remove(args.id)
            print("removed" if ok else f"no report with id {args.id}")
            return 0 if ok else 1
        status = STATUS_CONFIRMED if action == "confirm" else STATUS_CLEARED
        report = blacklist.set_status(args.id, status)
        if report is None:
            print(f"no report with id {args.id}")
            return 1
        print(f"{args.id} is now {status} (blocking: {report.blocks()})")
        return 0

    # ------------------------------------------------------------ reporting
    if action == "last":
        served = backend.last_served(args.player)
        if served is None:
            who = f" for player {args.player}" if args.player else ""
            print(f"no temple has been served yet{who}")
            print("Play the temple first, then report it.")
            return 1
        if served.get("archived"):
            report = blacklist.report_archived(
                served["dungeonID"], served["dungeonFloorNumber"],
                reason=args.reason, note=args.note or "", player_id=args.player,
            )
        else:
            fp = Fingerprint.from_response(served)
            report = blacklist.report_generated(
                fp, reason=args.reason, note=args.note or "", player_id=args.player,
            )
        print("reported the last temple served:")
        print(f"  {report.describe()}")
        print(f"  blocking: {report.blocks()}")
        print("\nIt will not be served again. A generated temple is replaced by")
        print("a different seed; an archived one is skipped in the rotation.")
        return 0

    if action == "add":
        if args.dungeon is None:
            print("add needs --dungeon <id> --floor <n>, or use: report last")
            return 2
        report = blacklist.report_archived(
            args.dungeon, args.floor, reason=args.reason,
            note=args.note or "", player_id=args.player,
        )
        print(f"reported: {report.describe()}")
        return 0

    print(f"unknown action {action!r}")
    return 2


def cmd_progress(args: argparse.Namespace) -> int:
    """Show each player's collection, derived the way the client derives it."""
    from .library import Library
    from .profile import ProfileStore

    library = Library(args.state / "assets", args.state / "fixtures")
    store = ProfileStore(args.state / "state" / "profiles", seed=library.user_response)
    players = store.known_players()
    if not players:
        print("no player profiles yet")
        return 0

    for player in players:
        profile = store.for_player(player)
        snap = profile.snapshot()
        relics = profile.relics_earned()
        whips = profile.whips_used()
        levels = profile.challenge_levels()
        cosmetics = profile.cosmetics_earned()

        print("\n" + "=" * 66)
        print(f"player {player}  ({snap.get('currentUsername')})")
        print(f"  reputation   : {snap.get('reputation')}")
        print(f"  currency     : {json.dumps(snap.get('currency'))}")
        print(f"  routes beaten: {len(snap.get('victoryRoutes') or [])}")
        print(f"  relics ({len(relics)}):")
        for line in _wrap(relics):
            print(f"      {line}")
        print(f"  whips used ({len(whips)}):")
        for line in _wrap(whips):
            print(f"      {line}")
        if levels:
            print("  highest challenge level per whip:")
            for whip, level in sorted(levels.items(), key=lambda x: -x[1]):
                extra = f"  -> {', '.join(cosmetics[whip])}" if whip in cosmetics else ""
                print(f"      {whip:12} {level:>5}{extra}")
        print(f"  purchases    : {len(snap.get('permanentPurchases') or [])}")
    return 0


def _wrap(items: list, width: int = 5) -> list[str]:
    return [
        ", ".join(items[i : i + width]) for i in range(0, len(items), width)
    ] or ["(none)"]


def cmd_daily(args: argparse.Namespace) -> int:
    """Capture today's daily temple, or say what is known about dailies.

    The daily is the one thing here with a deadline that cannot be made up
    later: there is no seed to derive it from -- every captured
    `GetDailyDungeonInfo` from the real service carries `dungeonSeed: null` --
    so a day not captured while the service is up is a day nobody can ever
    play again. Safe to run every day from a scheduler; capturing a daily
    already held does nothing.
    """
    from . import daily as daily_temples
    from .library import Library

    library = Library(args.state / "assets", args.state / "fixtures")

    if args.action == "backfill":
        learned = daily_temples.backfill(args.state)
        if not learned:
            print("nothing new to learn from the captures")
        for day, dungeon in sorted(learned.items()):
            print(f"  {day} was dungeon {dungeon}")
        return 0

    if args.action == "list":
        for line in daily_temples.describe(args.state, library):
            print(f"  {line}")
        days = daily_temples.recorded(args.state)
        if days:
            print()
            for day, dungeon in sorted(days.items())[-10:]:
                print(f"  {day}  dungeon {dungeon}")
        return 0

    # action == "capture"
    today = daily_temples.window()
    if daily_temples.recorded(args.state).get(today):
        print(f"today ({today}) is already captured -- nothing to do")
        return 0

    from .harvest import Harvester

    # What is held before asking, so that what arrives can be told apart from
    # what was already here. Recording an existing temple as today's daily
    # would be the archive asserting something nobody observed -- and it is
    # what happened the first time this ran, against a service that answered
    # 400 and returned nothing at all.
    before = set(daily_temples.candidates(library))

    harvester = Harvester(args.state, mode="DGM_DAILY", delay=args.delay)
    try:
        harvester.run(
            count=1,
            on_temple=lambda dungeon: print(f"  fetched dungeon {dungeon}"))
    except RuntimeError as exc:
        print(f"cannot reach the service: {exc}")
        return 1

    library = Library(args.state / "assets", args.state / "fixtures")
    arrived = sorted(set(daily_temples.candidates(library)) - before)
    if not arrived:
        print(f"nothing new arrived, so today ({today}) is not captured.")
        print("  The service refused the request or returned a temple already")
        print("  held. Credentials expire: run `serve --capture`, reach the")
        print("  main menu in game, and try again.")
        return 1

    caught = arrived[-1]
    daily_temples.record(args.state, today, caught)
    print(f"captured the daily for {today}: dungeon {caught}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Check the archive is intact and actually usable offline.

    A corrupt or truncated blob is worse than a missing one: it looks
    archived until the day the servers are gone and it is too late to
    re-fetch. The check itself lives in `integrity`, so the window can run
    it after a session without shelling out to this.
    """
    from . import integrity

    result = integrity.check(
        args.state, everything=not args.quick,
        on_progress=lambda message: print(f"  {message}"),
    )
    seen = result.checked + result.trusted
    print(f"blobs verified : {seen:,}/{result.total:,}"
          + (f"  ({result.trusted:,} unchanged since the last check)"
             if result.trusted else ""))
    print(f"layouts decoded: {result.layouts_ok:,}/{result.layouts_total:,}")
    if result.client_built:
        print(f"client-built   : {result.client_built} floor(s) had no "
              "layout upstream; the client generates these")
    print(f"took           : {result.seconds:.1f}s")

    if result.problems:
        print()
        print(f"{len(result.problems)} problem(s):")
        for line in result.problems[:25]:
            print(f"  - {line}")
        if len(result.problems) > 25:
            print(f"  ... and {len(result.problems) - 25} more")
        print()
        print("Re-run `archive` while the servers are still up to repair "
              "these.")
        return 1

    print()
    print("archive is intact")
    return 0


def cmd_scrub(args: argparse.Namespace) -> int:
    """Re-scrub captures written before redaction existed."""
    import json

    from . import redact

    changed = 0
    scanned = 0
    for path in sorted(args.state.rglob("*.jsonl")):
        lines: list[str] = []
        dirty = False
        for raw in path.read_text("utf-8").splitlines():
            if not raw.strip():
                continue
            scanned += 1
            try:
                entry = json.loads(raw)
            except ValueError:
                lines.append(raw)
                continue
            cleaned = redact.scrub(entry)
            if cleaned != entry:
                dirty = True
            lines.append(json.dumps(cleaned, ensure_ascii=False))
        if dirty:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            changed += 1
            print(f"scrubbed {path}")

    # The transcript is a derived, human-readable view; it cannot be safely
    # rewritten in place, so drop it and let the next capture regenerate it.
    transcript = args.state / "capture" / "transcript.txt"
    if transcript.exists():
        transcript.unlink()
        print(f"removed {transcript} (regenerates on next capture)")

    print(f"\n{scanned} record(s) scanned, {changed} file(s) rewritten")
    return 0


def cmd_restore(_args: argparse.Namespace) -> int:
    if client_config.restore():
        print(f"removed our settings from {client_config.ENGINE_INI}")
    else:
        print("nothing to undo -- Engine.ini has none of our settings")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """What is archived, and therefore what is playable offline."""
    from .library import Library
    from .session import SessionStore

    library = Library(args.state / "assets", args.state / "fixtures")
    assets = args.state / "assets"
    blobs = [p for p in assets.glob("*") if p.is_file() and p.name != "index.json"]
    total = sum(p.stat().st_size for p in blobs)

    print(f"archive : {library.summary()}")
    print(f"          {len(blobs)} blob(s), {total / 1_048_576:.1f} MB")

    if library.temples:
        print("\nplayable offline:")
        by_dungeon: dict[int, list] = {}
        for (dungeon, floor), temple in sorted(library.temples.items()):
            by_dungeon.setdefault(dungeon, []).append((floor, temple))
        incomplete = 0
        orphaned = 0
        for dungeon, floors in sorted(by_dungeon.items()):
            usable = [f for f, t in floors if t.layout and t.response]
            expected = library.floors_expected(dungeon)
            phantoms = sum(len(t.ghosts) for _, t in floors)
            listed = ", ".join(str(f) for f in sorted(usable)) or "none"

            if not usable:
                # Blobs downloaded but the GetDungeon response was never
                # recorded, so there is no seed or phantom metadata to replay.
                orphaned += 1
                print(
                    f"  dungeon {dungeon}: NOT REPLAYABLE (blobs only, no "
                    f"response captured), {phantoms} phantom blob(s)"
                )
                continue
            if expected is None:
                mark = f"{len(usable)} floor(s)"
            elif len(usable) >= expected:
                mark = f"all {expected} floors"
            else:
                mark = f"{len(usable)} of {expected} floors - INCOMPLETE"
                incomplete += 1

            print(
                f"  dungeon {dungeon}: {mark} [{listed}], {phantoms} phantom(s)"
            )

        if orphaned:
            print(
                f"\n{orphaned} temple(s) have blobs but no captured response, so "
                "they cannot be replayed. Their routes are closed and cannot be "
                "re-requested."
            )
        if incomplete:
            print(
                f"\n{incomplete} temple(s) missing floors. Reaching a floor is what "
                "archives it -- play deeper to finish them."
            )
    else:
        print("\nnothing archived yet -- play a temple with `serve --capture` running")

    session = SessionStore(args.state / "session")
    if session.load():
        state = "complete" if session.is_complete() else "incomplete"
        print(f"\ncredentials: {state} (captured {session.captured_at()})")

    transcript = args.state / "capture" / "transcript.txt"
    if transcript.exists():
        size = transcript.stat().st_size
        print(f"transcript : {transcript} ({size / 1024:.0f} KB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="phantom_offline", description=__doc__)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="run the offline server")
    p.add_argument("--port", type=int, default=54908)
    p.add_argument("--proxy-port", type=int, default=54909)
    p.add_argument(
        "--host", default="127.0.0.1",
        help="address to listen on. The default accepts nothing from the "
        "network; pass 0.0.0.0 to host for other players.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--capture",
        action="store_true",
        help="forward to the real servers and archive their responses "
        "(preservation mode -- only useful while they are still up)",
    )
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser(
        "play",
        help="patch, serve and launch the game in one step, then clean up",
        description="The whole session in one command. Use --listen while the "
        "real servers are alive to bank everything you play; use --host to "
        "play offline from what you have already kept.",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "--listen", action="store_true",
        help="talk to the real servers and keep everything (default)",
    )
    group.add_argument(
        "--host", action="store_true", help="play offline from what you have kept"
    )
    p.add_argument("--port", type=int, default=54908)
    p.add_argument("--proxy-port", type=int, default=54909)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--check", action="store_true",
                   help="report what would happen, then stop")
    p.add_argument("--no-game", action="store_true",
                   help="bring the server up but do not start the game")
    p.add_argument("game_args", nargs="*", help="extra arguments for the game")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser(
        "pool",
        help="share temples with other players, and take in theirs",
        description="A contribution is a folder of temples and phantoms plus a "
        "manifest. Hand it to someone, or take theirs. Only temples the real "
        "service minted travel; nothing generated offline, no profile, and no "
        "credentials.",
    )
    p.add_argument("action", choices=["status", "export", "import"],
                   help="status (default), export, import")
    p.add_argument("--out", type=Path, help="folder to write, for export")
    p.add_argument("--folder", type=Path, help="contribution to read, for import")
    p.add_argument("--against", type=Path,
                   help="a manifest of what they already have, to send only the rest")
    p.add_argument("--names", action="store_true",
                   help="(default) include the display names of phantom runners")
    p.add_argument("--no-names", action="store_true",
                   help="leave the runners' display names out of the export")
    p.set_defaults(func=cmd_pool)

    p = sub.add_parser(
        "window", help="open the two-button window (what the packaged build runs)"
    )
    p.set_defaults(func=cmd_window)

    p = sub.add_parser(
        "fidelity",
        help="check our answers against the real server's captured replies",
        description="Replays captured exchanges through the offline server and "
        "reports where the answers differ. Writes a summary that names fields "
        "and types only, so it can be shared without sharing anything personal.",
    )
    p.add_argument("--limit", type=int, help="stop after this many exchanges")
    p.set_defaults(func=cmd_fidelity)

    p = sub.add_parser(
        "bundles",
        help="shared temple collections you can switch on or off",
        description="Your own captures are always used. A bundle is extra "
        "content someone else gathered; bundles start switched off, so nothing "
        "arrives in your game without you choosing it.",
    )
    p.add_argument("--enable", metavar="NAME", help="switch a bundle on")
    p.add_argument("--disable", metavar="NAME", help="switch a bundle off")
    p.set_defaults(func=cmd_bundles)

    p = sub.add_parser("launch", help="configure and start the game")
    p.add_argument("--proxy-port", type=int, default=54909)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--via-steam",
        action="store_true",
        help="start through the Steam client instead of running the exe directly",
    )
    p.add_argument("game_args", nargs="*", help="extra args passed to the game")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("status", help="show captured traffic")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("restore", help="undo our Engine.ini changes")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser(
        "patch", help="point the game's backend URLs at the local server"
    )
    p.add_argument("--port", type=int, default=54908)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_patch)

    p = sub.add_parser("unpatch", help="restore the original executable")
    p.set_defaults(func=cmd_unpatch)

    p = sub.add_parser(
        "scrub", help="remove credentials from previously captured files"
    )
    p.set_defaults(func=cmd_scrub)

    p = sub.add_parser(
        "harvest",
        help="archive fresh temples from the live server without playing them",
    )
    p.add_argument("--count", type=int, default=5, help="temples to fetch")
    p.add_argument(
        "--mode",
        default="DGM_ADVENTURE",
        help="game mode to harvest (DGM_ADVENTURE or DGM_CLASSIC). DGM_DAILY "
        "cannot be bulk-harvested -- there is only one per day.",
    )
    p.add_argument(
        "--delay", type=float, default=2.0, help="seconds between requests"
    )
    p.add_argument(
        "--area",
        type=int,
        choices=[1, 2, 3, 4],
        help="area to harvest (Ruins/Caverns/Inferno/Rift). Sets "
        "difficultyRating accordingly - this is how deep-area temples are "
        "reached without progressing a route.",
    )
    p.add_argument(
        "--difficulty",
        type=int,
        default=0,
        help="raw difficultyRating, if you would rather set it directly. "
        "Must be a multiple of 20.",
    )
    p.add_argument(
        "--curse-level",
        type=int,
        default=0,
        help="curseLevel, shown in game as Challenge Level. Did not change "
        "which temple is returned in testing.",
    )
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_harvest)

    p = sub.add_parser(
        "shares",
        help="look up player share codes and archive the routes they shared",
    )
    p.add_argument("codes", nargs="*", help="share codes, or use --codes-file")
    p.add_argument(
        "--codes-file",
        type=Path,
        help="file of share codes; anything code-shaped is extracted",
    )
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument(
        "--any-length",
        action="store_true",
        help="accept 6-12 character codes instead of exactly 8",
    )
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_shares)

    p = sub.add_parser(
        "shareharvest",
        help="archive temples behind routes other players shared (run `shares` first)",
    )
    p.add_argument("--limit", type=int, help="stop after this many routes")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_shareharvest)

    p = sub.add_parser(
        "pin", help="force all fresh runs onto one archived temple"
    )
    p.add_argument("dungeon", nargs="?", type=int, help="dungeon id to pin")
    p.add_argument("--clear", action="store_true", help="remove the pin")
    p.set_defaults(func=cmd_pin)

    p = sub.add_parser(
        "resync",
        help="refresh a profile from the newest captured login for that account",
    )
    p.add_argument("--player", required=True, help="playerId (SteamID64)")
    p.add_argument("--user-id", required=True, help="numeric userID from captures")
    p.add_argument(
        "--from-state",
        type=Path,
        help="state directory holding the captures to sync FROM, when it is "
        "not the one holding the profile (captures usually live in the "
        "archive, while play happens elsewhere)",
    )
    p.set_defaults(func=cmd_resync)

    p = sub.add_parser(
        "progress", help="show each player's relics, whips and challenge levels"
    )
    p.set_defaults(func=cmd_progress)

    p = sub.add_parser(
        "daily",
        help="capture today's daily temple, or list what has been captured",
    )
    p.add_argument("action", choices=["capture", "list", "backfill"],
                   nargs="?",
                   default="list")
    p.add_argument("--delay", type=float, default=2.0)
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser(
        "verify", help="check archived blobs and layouts are intact and decodable"
    )
    p.add_argument("--quick", action="store_true",
                   help="only re-read what has changed since the last check")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser(
        "archive",
        help="download temple layouts and phantom recordings referenced by "
        "captured responses (do this while the CDN is still up)",
    )
    p.add_argument("--workers", type=int, default=6)
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser(
        "report",
        help="report a temple that cannot be finished, and manage the blacklist",
        description="Temples that cannot be finished are withheld from players. "
        "A generated one is replaced by rerolling its seed; an archived one is "
        "skipped, since its layout is fixed CDN bytes with no seed to reroll.",
    )
    p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=[
            "list", "last", "add", "confirm", "clear",
            "remove", "export", "import", "reasons",
        ],
        help="list (default), last, add, confirm, clear, remove, export, "
        "import, reasons",
    )
    p.add_argument(
        "--reason", default="other",
        help="why it is unplayable (see: report reasons)",
    )
    p.add_argument("--note", help="free text describing what went wrong")
    p.add_argument("--player", help="SteamID64 whose last temple to report")
    p.add_argument("--dungeon", type=int, help="archived dungeon id, for 'add'")
    p.add_argument("--floor", type=int, default=0, help="floor number, for 'add'")
    p.add_argument("--id", help="report id, for confirm/clear/remove")
    p.add_argument("--file", type=Path, help="export to merge, for 'import'")
    p.add_argument("--out", type=Path, help="where to write, for 'export'")
    p.set_defaults(func=cmd_report)


    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        args = ap.parse_args((argv or []) + ["serve"])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
