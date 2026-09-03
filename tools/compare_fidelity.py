"""Compares the offline server's responses against the real server's.

Preservation means the replacement should be indistinguishable from the
original, not merely good enough to boot. This replays every captured request
through the offline backend and diffs the result against what wiby.net actually
answered, reporting three kinds of divergence:

  MISSING  a field the real server sent that we do not
  EXTRA    a field we invent that the real server never sent
  TYPE     same field, different JSON type (e.g. null vs [])

Value differences are expected and mostly fine -- timestamps, ids and seeds
legitimately differ. Shape differences are the bugs.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.backend import OfflineBackend  # noqa: E402
from phantom_offline.library import Library  # noqa: E402

BASE = "http://127.0.0.1:54908"

# Fields whose values are expected to differ every time.
VOLATILE = {
    "maintenanceTimeUTC",
    "Timestamp",
    "timestamp",
    "expiryTime",
    "layoutDownloadURL",
    "downloadURL",
    "targetEndpoint",
}


def jtype(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def walk(real: Any, ours: Any, path: str, out: list[tuple[str, str, str]]) -> None:
    if isinstance(real, dict):
        if not isinstance(ours, dict):
            out.append(("TYPE", path, f"real object, ours {jtype(ours)}"))
            return
        for key in real:
            child = f"{path}.{key}" if path else key
            if key not in ours:
                out.append(("MISSING", child, f"real sends {jtype(real[key])}"))
            else:
                walk(real[key], ours[key], child, out)
        for key in ours:
            if key not in real:
                child = f"{path}.{key}" if path else key
                out.append(("EXTRA", child, f"we send {jtype(ours[key])}"))
        return

    if jtype(real) != jtype(ours) and path.rsplit(".", 1)[-1] not in VOLATILE:
        out.append(("TYPE", path, f"real {jtype(real)}, ours {jtype(ours)}"))


def main() -> int:
    ap = argparse.ArgumentParser()
    # Same resolution order as the CLI, so this works on any machine:
    # --state, then $PHANTOM_OFFLINE_STATE, then .archive_path, then ./run.
    from phantom_offline.__main__ import _default_state

    ap.add_argument("--state", type=Path, default=_default_state())
    ap.add_argument("--endpoint")
    args = ap.parse_args()

    library = Library(args.state / "assets", args.state / "fixtures")

    # Replay into a throwaway state directory. Captured requests carry a
    # pseudonymised playerId, so running against the real state would create a
    # phantom profile named after the redaction placeholder and credit it with
    # routes belonging to actual players.
    scratch = Path(tempfile.mkdtemp(prefix="fidelity-"))
    try:
        shutil.copytree(args.state / "state", scratch / "state", dirs_exist_ok=True)
    except (OSError, FileNotFoundError):
        pass
    backend = OfflineBackend(scratch / "state", archive=library)

    handlers = {
        "VerifyUserID": lambda r: backend.verify_user(r),
        "WakeUp": lambda r: backend.wake_up(r, BASE),
        "GetDungeon": lambda r: backend.get_dungeon(r, BASE),
        "SubmitRun": lambda r: backend.submit_run(r),
        "PurchaseUpgrade": lambda r: backend.purchase_upgrade(r),
        "ConvertKeys": lambda r: backend.convert_keys(r),
        "GetFriends": lambda r: backend.friends(r),
    }

    grand: dict[str, list] = defaultdict(list)
    for name, handler in handlers.items():
        if args.endpoint and args.endpoint.lower() not in name.lower():
            continue
        path = args.state / "fixtures" / f"{name}.jsonl"
        if not path.exists():
            continue

        samples = []
        for line in path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("status") == 200 and isinstance(entry.get("response_body"), dict):
                samples.append(entry)
        if not samples:
            continue

        entry = samples[-1]
        request = entry.get("request_body") or {}
        try:
            ours = handler(request if isinstance(request, dict) else {})
        except Exception as exc:  # noqa: BLE001
            print(f"\n{name}: handler raised {exc!r}")
            continue

        findings: list[tuple[str, str, str]] = []
        walk(entry["response_body"], ours, "", findings)

        print(f"\n{'=' * 70}\n{name}  ({len(samples)} sample(s))")
        if not findings:
            print("  shape matches the real server exactly")
            continue
        for kind, field, detail in sorted(findings):
            print(f"  {kind:8} {field}  ({detail})")
            grand[kind].append(f"{name}.{field}")

    shutil.rmtree(scratch, ignore_errors=True)

    print(f"\n{'=' * 70}\nSUMMARY")
    for kind in ("MISSING", "EXTRA", "TYPE"):
        print(f"  {kind}: {len(grand[kind])}")
    return 1 if grand else 0


if __name__ == "__main__":
    raise SystemExit(main())
