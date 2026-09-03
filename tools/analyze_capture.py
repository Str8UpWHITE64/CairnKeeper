"""Compare a live capture against what the offline server would have served.

Point it at a capture state directory and it reports, in order:

1. every endpoint the client used, flagging any we have never seen before
2. the route the client walked, floor by floor
3. a field-by-field diff of each real /GetDungeon against our own answer
4. the full verbatim response for the deepest floor reached

(4) is the point of the exercise: a temple's last floor is where the exit door
and its key are placed, and an offline run got stuck on one. If the real server
says something there that we do not, this is where it shows up.

    python tools/analyze_capture.py E:\\PhantomAbyssKeyDoor
"""
from __future__ import annotations

import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

# Endpoints we have already reconstructed. Anything outside this set is new and
# is the most interesting thing a capture can contain.
KNOWN = {
    "WakeUp", "VerifyUserID", "GetDungeon", "SubmitDungeonLayout", "SubmitRun",
    "GetFriends", "AddFriend", "PurchaseUpgrade", "SpendCurrency",
    "ConvertKeys", "GetDailyDungeonInfo", "VerifyShareCode", "ResetData",
    "RefreshVerification", "redirectV3.json",
}

# Values that legitimately differ between the real server and ours: identifiers
# it allocates, counters, and other players' phantoms.
IDENTIFIERS = {
    "dungeonID", "dungeonSeed", "routeID", "userID",
    "totalDungeonAttemptsAllFloors", "playerCount", "deathCount",
    "ghostRuns", "serverStatus",
}


def load(fixtures: Path, name: str) -> list[dict]:
    path = fixtures / f"{name}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text("utf-8", errors="replace").splitlines()
        if line.strip()
    ]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    state = Path(argv[0])
    fixtures = state / "fixtures"
    if not fixtures.exists():
        print(f"no fixtures under {state}")
        return 1

    # ---------------------------------------------------------- endpoints
    print("=" * 70)
    print("ENDPOINTS USED")
    print("=" * 70)
    names = sorted(p.stem for p in fixtures.glob("*.jsonl"))
    for name in names:
        rows = load(fixtures, name)
        statuses = Counter(r.get("status") for r in rows)
        detail = ", ".join(f"{s}x{n}" for s, n in sorted(statuses.items(), key=str))
        flag = "" if name in KNOWN else "   <-- NOT SEEN BEFORE"
        print(f"  {name:<24} {len(rows):>4} exchange(s)  [{detail}]{flag}")
    unknown = [n for n in names if n not in KNOWN]
    if unknown:
        print(f"\n  !! new endpoints: {', '.join(unknown)}")
        print("     These are unreconstructed -- inspect them before anything else.")

    dungeons = load(fixtures, "GetDungeon")
    if not dungeons:
        print("\nno /GetDungeon exchanges captured")
        return 0

    # -------------------------------------------------------------- route
    print()
    print("=" * 70)
    print("ROUTE WALKED")
    print("=" * 70)
    hdr = (f"{'area':>5} {'flr':>4} {'set':>4} {'lt':>3} {'ft':>3} {'tot':>4} "
           f"{'ghosts':>7} {'dungeon':>8}  layout")
    print(hdr)
    print("-" * len(hdr))
    for row in dungeons:
        r = row.get("response_body") or {}
        print(f"{str(r.get('areaID')):>5} {str(r.get('dungeonFloorNumber')):>4} "
              f"{str(r.get('dungeonSettingsIndex')):>4} "
              f"{str(r.get('dungeonLayoutType')):>3} "
              f"{str(r.get('dungeonFloorType')):>3} "
              f"{str(r.get('dungeonFloorTotalCount')):>4} "
              f"{len(r.get('ghostRuns') or []):>7} {str(r.get('dungeonID')):>8}  "
              f"{'CDN' if r.get('layoutDownloadURL') else 'client-generated'}")

    # --------------------------------------------------------------- diff
    print()
    print("=" * 70)
    print("REAL vs OFFLINE")
    print("=" * 70)
    scratch = Path(tempfile.mkdtemp())
    (scratch / "assets").mkdir(parents=True)
    backend = OfflineBackend(
        scratch / "state", archive=Library(scratch / "assets", scratch / "fx")
    )
    remap: dict[int, int] = {}
    divergences: Counter = Counter()
    for i, row in enumerate(dungeons, 1):
        req = dict(row.get("request_body") or {})
        real = row.get("response_body") or {}
        if req.get("dungeonId"):
            req["dungeonId"] = remap.get(req["dungeonId"], req["dungeonId"])
        ours = backend.get_dungeon(req, "http://127.0.0.1:54908")
        if real.get("dungeonID"):
            remap[real["dungeonID"]] = ours["dungeonID"]
        diffs = sorted(
            k for k in set(real) | set(ours)
            if k not in IDENTIFIERS and real.get(k) != ours.get(k)
        )
        for k in diffs:
            divergences[k] += 1
        label = (f"#{i} area {real.get('areaID')} "
                 f"floor {real.get('dungeonFloorNumber')}")
        if not diffs:
            print(f"  {label}: match")
        else:
            print(f"  {label}: DIFFERS")
            for k in diffs:
                print(f"      {k}")
                print(f"        real: {json.dumps(real.get(k))[:150]}")
                print(f"        ours: {json.dumps(ours.get(k))[:150]}")

    print(f"\n  non-identifier divergences: {sum(divergences.values())}")
    for k, n in divergences.most_common():
        print(f"     {k}: {n}")

    # ------------------------------------------------------- deepest floor
    deepest = max(
        dungeons,
        key=lambda r: (
            (r.get("response_body") or {}).get("areaID") or 0,
            (r.get("response_body") or {}).get("dungeonFloorNumber") or 0,
        ),
    )
    body = deepest.get("response_body") or {}
    print()
    print("=" * 70)
    print(f"DEEPEST FLOOR REACHED -- area {body.get('areaID')} "
          f"floor {body.get('dungeonFloorNumber')} of "
          f"{body.get('dungeonFloorTotalCount')}")
    print("=" * 70)
    trimmed = dict(body)
    ghosts = trimmed.get("ghostRuns") or []
    if ghosts:
        trimmed["ghostRuns"] = f"<{len(ghosts)} phantom(s)>"
    print(json.dumps(trimmed, indent=2))

    # ---------------------------------------------------------- run submits
    runs = load(fixtures, "SubmitRun")
    if runs:
        print()
        print("=" * 70)
        print("RUN SUBMISSIONS")
        print("=" * 70)
        for row in runs:
            q = row.get("request_body") or {}
            r = row.get("response_body") or {}
            print(f"  status={row.get('status')} success={q.get('success')} "
                  f"dungeon={q.get('dungeonId')} floor={q.get('dungeonFloorNumber')}"
                  f"  earned={q.get('currency')}")
            if r.get("currency") is not None:
                print(f"      banked -> {r.get('currency')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
