"""The daily temple: one temple, everybody, changing at midnight UTC.

That is the whole of what makes a daily a daily. Take away "everybody plays the
same one" and it is an ordinary temple with a timer on it, which is what the
offline server was serving: the rotation counter handed each request the next
temple in the list, so two players on the same day got different dailies and one
player got a different one every attempt.

There is no seed to derive it from. Every captured `GetDailyDungeonInfo` from
the real service carries `dungeonSeed: null` -- the daily arrives like any other
temple, through `GetDungeon` with `gameMode: DGM_DAILY`, and the service decides
which one. So preserving dailies means capturing them on the day, and replaying
them afterwards in an order everyone agrees on.

Two rules, in order:

* If a daily was captured on that date, that date gets that temple. It is the
  real answer, and there is no reason to invent one.
* Otherwise pick from the daily temples that were captured, by hashing the
  date. Arbitrary, but arbitrary in the same way on every machine holding the
  same archive -- which is the property that matters.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

LEDGER = "dailies.json"


def window(when: datetime | None = None) -> str:
    """Which daily is current. Midnight UTC, matching the service's expiry."""
    now = when or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).date().isoformat()


def expires_after(day: str) -> datetime:
    """When this daily gives way to the next one."""
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return start + timedelta(days=1)


def _path(state_dir: Path) -> Path:
    return Path(state_dir) / LEDGER


def recorded(state_dir: Path) -> dict[str, int]:
    """Dates whose real daily is known, and which temple it was."""
    try:
        data = json.loads(_path(state_dir).read_text("utf-8"))
        if isinstance(data, dict):
            return {str(k): int(v) for k, v in (data.get("days") or {}).items()}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def record(state_dir: Path, day: str, dungeon_id: int) -> bool:
    """Remember that this was the daily on this date.

    Only ever written from a live capture. A temple we picked ourselves must
    not be recorded as though the service had chosen it, or the archive starts
    asserting things it does not know.
    """
    if not day or not dungeon_id or dungeon_id <= 0:
        return False
    days = recorded(state_dir)
    if days.get(day) == int(dungeon_id):
        return False
    days[day] = int(dungeon_id)
    try:
        path = _path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"days": dict(sorted(days.items()))}, indent=2),
                       encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


def candidates(library: Any) -> list[int]:
    """Archived daily temples that can actually be played end to end.

    A daily that dead-ends on floor two is worse than a substitute, so only
    temples with every floor archived are offered.
    """
    from .backend import GAME_MODES

    daily_mode = GAME_MODES["DGM_DAILY"]
    complete = set(library.complete_dungeons())
    found = {
        dungeon
        for (dungeon, _), temple in library.temples.items()
        if dungeon in complete
        and (temple.response or {}).get("gameMode") == daily_mode
    }
    return sorted(found)


def choose(state_dir: Path, library: Any, day: str | None = None) -> int | None:
    """Which temple is the daily on this date, for everyone on this archive."""
    day = day or window()

    known = recorded(state_dir).get(day)
    if known and library.lookup(known, 0) is not None:
        return known

    pool = candidates(library)
    if not pool:
        return None
    # Deterministic from the date alone. Two people holding the same archive
    # agree without talking to each other, which is the only way a daily can
    # work once there is no service to be the authority.
    digest = hashlib.sha256(f"daily:{day}".encode("utf-8")).digest()
    return pool[int.from_bytes(digest[:8], "big") % len(pool)]


def backfill(state_dir: Path) -> dict[str, int]:
    """Recover which day each captured daily belonged to, from the captures.

    Nothing recorded this at the time, but the capture itself is the evidence:
    a daily was requested on a date and the service answered with a temple, so
    that temple was the daily for that date. The response's own timestamp says
    which date, in the same UTC the daily window turns over in.

    Returns what was newly learned. Existing records are never overwritten --
    a date already known was recorded from a live capture and is at least as
    good as this.
    """
    fixtures = Path(state_dir) / "fixtures" / "GetDungeon.jsonl"
    if not fixtures.is_file():
        return {}

    from .backend import GAME_MODES

    daily_mode = GAME_MODES["DGM_DAILY"]
    known = recorded(state_dir)
    learned: dict[str, int] = {}
    for line in fixtures.read_text("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        body = entry.get("response_body")
        if not isinstance(body, dict) or body.get("gameMode") != daily_mode:
            continue
        dungeon = body.get("dungeonID")
        stamp = entry.get("ts")
        if not dungeon or not stamp:
            continue
        try:
            when = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        day = window(when)
        if day in known or day in learned:
            continue
        learned[day] = int(dungeon)

    for day, dungeon in learned.items():
        record(state_dir, day, dungeon)
    return learned


def describe(state_dir: Path, library: Any) -> list[str]:
    """What is known about dailies, for somebody deciding whether to capture."""
    days = recorded(state_dir)
    pool = candidates(library)
    lines = [
        f"daily temples archived : {len(pool)}",
        f"dates with a real one  : {len(days)}",
    ]
    if days:
        first, last = min(days), max(days)
        lines.append(f"captured between       : {first} and {last}")
    today = window()
    picked = choose(state_dir, library, today)
    if picked is None:
        lines.append(f"today ({today})   : nothing to serve")
    else:
        how = "captured on the day" if days.get(today) == picked else "chosen by date"
        lines.append(f"today ({today})   : dungeon {picked} ({how})")
    return lines
