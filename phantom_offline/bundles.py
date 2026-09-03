"""Shared content a player may accept, or may not.

A player's own captures are theirs. A bundle is somebody else's work offered to
them -- harvested temples, or one day whatever the pool has gathered -- and
whether to accept it is their call.

Both answers are reasonable, and they are different games. Some people want
their offline copy to be exactly what they played, with their own ghosts in it
and nothing else. Others want every temple anyone managed to save before the
service went. Neither is more correct, so the client asks rather than assumes.

A bundle is a directory of blobs named the way the CDN named them, so the
existing index reads it with no special handling:

    <state>/bundles/
        community-2026-08/
            version_128__2026-08-15__dungeon-747386-floor-0-layout-abcd1234
            ...
            bundle.json          (optional: name, description, counts)

Bundles are read-only and never merged into the player's own directory, so
turning one off is instant and cannot disturb what they captured themselves.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

BUNDLES_DIR = "bundles"
SETTINGS_FILE = "bundles.json"


@dataclass
class Bundle:
    path: Path
    name: str
    description: str = ""
    blobs: int = 0
    enabled: bool = True

    @property
    def key(self) -> str:
        return self.path.name


def _settings_path(state_dir: Path) -> Path:
    return Path(state_dir) / SETTINGS_FILE


def _read_settings(state_dir: Path) -> dict:
    try:
        data = json.loads(_settings_path(state_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings(state_dir: Path, data: dict) -> None:
    path = _settings_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def discover(state_dir: Path) -> list[Bundle]:
    """Every bundle present, and whether the player has it switched on.

    A bundle appearing for the first time is off. Content arriving in someone's
    game without them choosing it is the thing this whole setting exists to
    prevent, so the default is the conservative one.
    """
    root = Path(state_dir) / BUNDLES_DIR
    if not root.exists():
        return []

    settings = _read_settings(state_dir)
    known = settings.get("enabled") or {}

    found = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        meta = {}
        manifest = path / "bundle.json"
        if manifest.exists():
            try:
                loaded = json.loads(manifest.read_text("utf-8"))
                if isinstance(loaded, dict):
                    meta = loaded
            except (OSError, ValueError):
                pass
        blobs = sum(1 for f in path.iterdir() if f.is_file() and f.name != "bundle.json")
        found.append(
            Bundle(
                path=path,
                name=str(meta.get("name") or path.name),
                description=str(meta.get("description") or ""),
                blobs=blobs,
                enabled=bool(known.get(path.name, False)),
            )
        )
    return found


def enabled_paths(state_dir: Path) -> list[Path]:
    """The directories host mode should read alongside the player's own."""
    return [b.path for b in discover(state_dir) if b.enabled]


def set_enabled(state_dir: Path, key: str, enabled: bool) -> bool:
    """Switch one bundle on or off. Returns False if there is no such bundle."""
    available = {b.key for b in discover(state_dir)}
    if key not in available:
        return False
    settings = _read_settings(state_dir)
    known = dict(settings.get("enabled") or {})
    known[key] = bool(enabled)
    settings["enabled"] = known
    _write_settings(state_dir, settings)
    return True


def describe(state_dir: Path) -> list[str]:
    """Bundles as lines a player can read."""
    found = discover(state_dir)
    if not found:
        return [
            "No shared content installed.",
            f"Bundles go in {Path(state_dir) / BUNDLES_DIR}, one folder each.",
        ]
    lines = []
    for bundle in found:
        mark = "on " if bundle.enabled else "off"
        lines.append(f"  [{mark}] {bundle.key:<28} {bundle.blobs:>7,} file(s)")
        if bundle.description:
            lines.append(f"        {bundle.description}")
    return lines
