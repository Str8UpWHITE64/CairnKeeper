"""Check the offline server against what the real one actually said.

Every captured exchange is a test case nobody had to write: replay the request
through the offline backend and compare the answer to what wiby.net gave. This
single technique found six real bugs in one afternoon, four of which a fully
green test suite had been hiding -- because a test can only assert what its
author already believed.

The point of running it on players' machines is that they will reach corners of
the protocol this project never has. Somebody who plays Daily every morning, or
who owns whips nobody here has, produces traffic that would otherwise never be
seen until the service is gone and it is too late to look.

What a report may contain
-------------------------
Field names, JSON types, and small enum-ish numbers. That is enough to find a
divergence and nowhere near enough to identify anybody.

It must never contain: player ids, usernames, tokens, share codes, seeds,
layout data, phantom recordings, or free text of any kind. The rule is a
whitelist rather than a blocklist -- a value is dropped unless it is provably
harmless, so a field added to the protocol later cannot leak by default.
"""
from __future__ import annotations

import json
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MISSING = "missing"      # the real server sent a field we do not
EXTRA = "extra"          # we invent a field it never sent
TYPE = "type"            # same field, different JSON type
VALUE = "value"          # same field and type, different small value

# Values that legitimately differ every time and mean nothing when they do.
VOLATILE = frozenset({
    "maintenanceTimeUTC", "Timestamp", "timestamp", "expiryTime",
    "layoutDownloadURL", "downloadURL", "dungeonSeed", "routeID", "dungeonID",
    "userID", "runID", "totalDungeonAttemptsAllFloors", "shareCode",
    "playerCount", "deathCount", "ghostRuns", "runData", "savedLayoutData",
})

# The only values that may travel. Small integers describing shape, and
# booleans. Anything else is summarized as its type and nothing more.
SAFE_VALUE_MAX = 4096

# Fields whose contents are data rather than structure. Their dictionary keys
# are player names, whip names, damage types -- values wearing the costume of
# field names. Descending into one puts other people's identities into a report
# meant to be shareable, which is precisely what it must never contain.
#
# `playerStats` is opaque wholesale rather than by listing its children: every
# stat under it is data-keyed, and naming them individually would mean a stat
# added later leaks by default.
OPAQUE = frozenset({"playerStats"})

# A path segment is only allowed through if it looks like a protocol field:
# letters and digits, starting with a letter. Names, ids and free text do not
# survive that, so even a new data-keyed map cannot leak by accident.
import re as _re

_SCHEMA_SEGMENT = _re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _is_schema_path(path: str) -> bool:
    return all(_SCHEMA_SEGMENT.match(part) for part in path.split(".") if part)


@dataclass
class Divergence:
    endpoint: str
    field_path: str
    kind: str
    real: str = ""
    ours: str = ""
    count: int = 1

    def key(self) -> tuple:
        return (self.endpoint, self.field_path, self.kind)


@dataclass
class Report:
    checked: int = 0
    endpoints: Counter = field(default_factory=Counter)
    divergences: list[Divergence] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.divergences

    def summary(self) -> str:
        if not self.checked:
            return "nothing captured to check against"
        if self.clean:
            return f"{self.checked} exchange(s) checked, no divergences"
        return (
            f"{self.checked} exchange(s) checked, "
            f"{len(self.divergences)} divergence(s)"
        )

    def export(self) -> dict:
        """A report safe to hand to a stranger.

        Carries no identifiers, no traffic and no free text -- only which
        endpoint disagreed, which field, and how.
        """
        return {
            "schema": 1,
            "checked": self.checked,
            "endpoints": dict(self.endpoints),
            # Belt and braces: even if the walk let something through, a path
            # that does not look like a chain of protocol field names does not
            # leave this machine.
            "divergences": [
                {
                    "endpoint": d.endpoint,
                    "field": d.field_path,
                    "kind": d.kind,
                    "real": d.real,
                    "ours": d.ours,
                    "count": d.count,
                }
                for d in sorted(self.divergences, key=lambda d: (-d.count, d.key()))
                if _is_schema_path(d.field_path)
            ],
        }


def _shape(value: Any, key: str = "") -> str:
    """Describe a value without disclosing it.

    Numbers small enough to be an enum are named outright, because that is
    exactly the class of bug worth finding -- a layoutType of 1 where the real
    server sends 0. Everything else becomes its type.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        if key in VOLATILE:
            return "int"
        return str(value) if -SAFE_VALUE_MAX <= value <= SAFE_VALUE_MAX else "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string" if value else "empty-string"
    if isinstance(value, list):
        return f"list[{len(value)}]" if len(value) < 4 else "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _walk(real: Any, ours: Any, path: str, out: list, endpoint: str) -> None:
    if isinstance(real, dict) and isinstance(ours, dict):
        # Inside a data-keyed map the keys are values -- player names, whips,
        # damage types. Compare it as a whole rather than naming its contents.
        leaf = path.rsplit(".", 1)[-1] if path else ""
        if leaf in OPAQUE or any(p in OPAQUE for p in path.split(".")):
            if len(real) != len(ours):
                out.append(Divergence(endpoint, path, TYPE,
                                      real=f"object[{len(real)}]",
                                      ours=f"object[{len(ours)}]"))
            return
        for key in sorted(set(real) | set(ours)):
            where = f"{path}.{key}" if path else key
            if key not in ours:
                out.append(Divergence(endpoint, where, MISSING,
                                      real=_shape(real[key], key)))
            elif key not in real:
                out.append(Divergence(endpoint, where, EXTRA,
                                      ours=_shape(ours[key], key)))
            else:
                _walk(real[key], ours[key], where, out, endpoint)
        return

    leaf = path.rsplit(".", 1)[-1]
    if leaf in VOLATILE:
        return

    if type(real) is not type(ours) and not (
        isinstance(real, (int, float)) and isinstance(ours, (int, float))
    ):
        out.append(Divergence(endpoint, path, TYPE,
                              real=_shape(real, leaf), ours=_shape(ours, leaf)))
        return

    # Shape divergences are not the only bugs, and were not the worst ones.
    # Serving dungeonLayoutType 1 where the real server sends 0, or settings
    # index 0 for every Abyss area, are both the right type and the wrong
    # answer -- and both made temples that could not be finished. Small
    # integers are already judged safe to disclose, so this costs no privacy.
    if isinstance(real, int) and isinstance(ours, int) and real != ours:
        if abs(real) <= SAFE_VALUE_MAX and abs(ours) <= SAFE_VALUE_MAX:
            out.append(Divergence(endpoint, path, VALUE,
                                  real=str(real), ours=str(ours)))


def compare(state_dir: Path, *, limit: int | None = None) -> Report:
    """Replay captured exchanges through the offline backend and diff them."""
    from .backend import OfflineBackend
    from .library import Library

    report = Report()
    fixtures = Path(state_dir) / "fixtures"
    if not fixtures.exists():
        return report

    # A scratch backend, so checking never disturbs the player's own state.
    scratch = Path(tempfile.mkdtemp(prefix="fidelity-"))
    (scratch / "assets").mkdir(parents=True, exist_ok=True)
    library = Library(Path(state_dir) / "assets", fixtures)
    backend = OfflineBackend(scratch / "state", archive=library)

    handlers = {
        "getdungeon": backend.get_dungeon,
        "verifyuserid": backend.verify_user,
        "wakeup": getattr(backend, "wake_up", None),
    }

    seen: dict[tuple, Divergence] = {}
    for path in sorted(fixtures.glob("*.jsonl")):
        endpoint = path.stem
        handler = handlers.get(endpoint.lower())
        if handler is None:
            continue
        for line in path.read_text("utf-8", errors="replace").splitlines():
            if limit and report.checked >= limit:
                break
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("status") != 200:
                continue
            real = row.get("response_body")
            request = row.get("request_body")
            if not isinstance(real, dict) or not isinstance(request, dict):
                continue

            try:
                if endpoint.lower() == "getdungeon":
                    ours = handler(dict(request), "http://127.0.0.1:54908")
                else:
                    ours = handler(dict(request))
            except Exception:  # noqa: BLE001 - a crash is itself worth noting
                report.endpoints[endpoint] += 1
                report.checked += 1
                continue
            if not isinstance(ours, dict):
                continue

            # Only compare like with like. A replay does not share the live
            # server's route history, so if we answered with a different temple
            # than it did, every shape field differs for a reason that says
            # nothing about our fidelity. Comparing those anyway drowns the real
            # divergences in noise -- 1,152 reports of an areaID that was never
            # wrong.
            if real.get("dungeonID") and ours.get("dungeonID") != real.get("dungeonID"):
                continue

            report.checked += 1
            report.endpoints[endpoint] += 1
            found: list[Divergence] = []
            _walk(real, ours, "", found, endpoint)
            for div in found:
                existing = seen.get(div.key())
                if existing:
                    existing.count += 1
                else:
                    seen[div.key()] = div

    report.divergences = list(seen.values())
    return report


def save(state_dir: Path, report: Report) -> Path:
    """Keep the latest report where the player can find and inspect it."""
    path = Path(state_dir) / "fidelity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report.export(), indent=2), encoding="utf-8")
    tmp.replace(path)
    return path
