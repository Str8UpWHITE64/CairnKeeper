"""Checking that what was archived is still what was archived.

A truncated file looks archived right up until you need it, and by then the
service it came from is gone. So the check has to happen while there is still
something to re-download from -- which means it has to be cheap enough to run
without thinking about it.

The full sweep reads everything: 11,824 blobs and 1,713 layouts took nearly
four minutes on a 6.4 GB archive, which is fine to ask for deliberately and far
too slow to run after every session. But a session only adds a few dozen files,
and those are the only ones that can have changed since the last check. Keeping
a ledger of what was checked, and when, turns the after-session pass into a
second or two while still leaving nothing unchecked.

The ledger is a convenience, never an authority: anything it does not recognize
is read in full, and `everything=True` ignores it entirely.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

LEDGER = "integrity.json"


@dataclass
class Check:
    """What was looked at, and what was wrong with it."""

    checked: int = 0        # blobs read and hashed this time
    trusted: int = 0        # unchanged since the last check, so not re-read
    total: int = 0
    layouts_ok: int = 0
    layouts_total: int = 0
    client_built: int = 0   # floors the service never had a layout for
    problems: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        """One line, always said -- silence is indistinguishable from failure."""
        if not self.total and not self.layouts_total:
            return "Nothing archived yet, so nothing to check."
        looked = f"{self.checked:,} checked"
        if self.trusted:
            looked += f", {self.trusted:,} unchanged since last time"
        layouts = f"{self.layouts_ok:,}/{self.layouts_total:,} layouts readable"
        if self.ok:
            return f"Archive checked: {looked}; {layouts}. Everything intact."
        return (f"Archive checked: {looked}; {layouts}. "
                f"{len(self.problems)} problem(s) found.")


def _ledger_path(state_dir: Path) -> Path:
    return Path(state_dir) / LEDGER


def _load_ledger(state_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(_ledger_path(state_dir).read_text("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"files": {}}


def _save_ledger(state_dir: Path, ledger: dict[str, Any]) -> None:
    try:
        path = _ledger_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # Losing the ledger costs time on the next check and nothing else.
        pass


def check(
    state_dir: Path,
    *,
    everything: bool = False,
    on_progress: Callable[[str], None] | None = None,
) -> Check:
    """Read the archive back and say whether it is what it claims to be."""
    started = time.perf_counter()
    state_dir = Path(state_dir)
    assets = state_dir / "assets"
    result = Check()

    index_path = assets / "index.json"
    if not index_path.exists():
        result.seconds = time.perf_counter() - started
        return result
    try:
        index = json.loads(index_path.read_text("utf-8"))
    except (OSError, ValueError):
        result.problems.append("the archive index cannot be read")
        result.seconds = time.perf_counter() - started
        return result

    ledger = {"files": {}} if everything else _load_ledger(state_dir)
    known = ledger["files"]
    fresh: dict[str, Any] = {}
    result.total = len(index)

    for url, meta in index.items():
        if "error" in meta:
            result.problems.append(f"never downloaded: {url} ({meta['error']})")
            continue
        name = meta.get("file")
        path = assets / str(name)
        if not path.exists():
            result.problems.append(f"missing file: {name}")
            continue

        stat = path.stat()
        seen = known.get(str(name))
        if (seen and seen.get("bytes") == stat.st_size
                and seen.get("mtime") == int(stat.st_mtime)
                and seen.get("sha256") == meta.get("sha256")):
            # Same size, same timestamp, same expectation as when it was last
            # read in full. Re-reading it would produce the same answer.
            result.trusted += 1
            fresh[str(name)] = seen
            continue

        data = path.read_bytes()
        if len(data) != meta.get("bytes"):
            result.problems.append(
                f"size mismatch: {name} "
                f"({len(data)} on disk, {meta.get('bytes')} recorded)"
            )
            continue
        if meta.get("sha256") and hashlib.sha256(data).hexdigest() != meta["sha256"]:
            result.problems.append(f"checksum mismatch: {name}")
            continue
        result.checked += 1
        fresh[str(name)] = {
            "bytes": stat.st_size,
            "mtime": int(stat.st_mtime),
            "sha256": meta.get("sha256"),
        }
        if on_progress and result.checked % 2000 == 0:
            on_progress(f"checked {result.checked:,} files…")

    # A layout that will not decode is a temple that cannot be rebuilt.
    from .library import Library

    library = Library(assets, state_dir / "fixtures")
    result.layouts_total = len(library.temples)
    # Layouts whose bytes were vouched for above, and which decoded cleanly
    # last time. A layout that failed is never in here, so a broken one is
    # reported on every run until it is fixed.
    trusted_layouts = {
        name for name, seen in fresh.items()
        if seen.get("decoded") and known.get(name, {}).get("decoded")
    }
    for (dungeon, floor), temple in sorted(library.temples.items()):
        if not temple.layout:
            # Only a gap if the service ever offered one. A freshly minted
            # temple answers with an empty layout URL because nothing exists
            # upstream yet; offline the client builds the floor from the seed.
            if not (temple.response or {}).get("layoutDownloadURL"):
                result.client_built += 1
                result.layouts_ok += 1
                continue
            result.problems.append(
                f"dungeon {dungeon} floor {floor}: no layout archived")
            continue
        if temple.layout in trusted_layouts:
            # Its bytes are unchanged since a check that decoded it, so
            # decoding it again asks a question already answered. Without this
            # the incremental pass still read every layout in the archive,
            # which is most of what it costs.
            result.layouts_ok += 1
            continue
        try:
            raw = (assets / temple.layout).read_bytes()
            decoded = json.loads(base64.b64decode(raw))
            if not decoded.get("roomInfos"):
                raise ValueError("no roomInfos")
            result.layouts_ok += 1
            fresh.setdefault(str(temple.layout), {}).update({"decoded": True})
        except Exception as exc:  # noqa: BLE001
            result.problems.append(
                f"dungeon {dungeon} floor {floor}: unreadable layout ({exc})")

    _save_ledger(state_dir, {"files": fresh, "at": time.time()})
    result.seconds = time.perf_counter() - started
    return result
