"""Holds the live credentials needed to talk to the real server.

Calling ``/GetDungeon`` requires a valid Steam authentication ticket, which only
the game can obtain. During a capture the client sends one; this module keeps
the most recent set so the bulk archiver can reuse it.

Kept deliberately separate from ``fixtures/`` and ``capture/``:

* those are scrubbed and meant to be shareable,
* this file is **not** shareable -- it contains a live credential.

Steam auth tickets are short lived. Expect to re-capture before a long pull.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WARNING = (
    "This file contains a live Steam authentication ticket. "
    "Do not share it or commit it."
)

# Fields the server needs to accept a request as authenticated.
CREDENTIAL_FIELDS = (
    "userId",
    "playerId",
    "platform",
    "verificationToken",
    "platformVerificationId",
    "clientDungeonVersion",
    "clientProtocolVersion",
)


class SessionStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "credentials.json"
        self._lock = threading.Lock()

    def remember(self, body: dict[str, Any]) -> bool:
        """Record credentials from an outgoing request. True if updated.

        Merges rather than replaces. The client sends a partial set early on --
        ``/WakeUp`` carries a token but an empty ``platformVerificationId``,
        which the server rejects with 400 on ``/GetDungeon``. Overwriting a
        populated value with a later empty one loses the only usable set, so a
        non-empty value is never downgraded.
        """
        if not isinstance(body, dict):
            return False
        token = body.get("verificationToken")
        if not isinstance(token, str) or not token:
            return False

        with self._lock:
            creds = self._read_raw()
            # A different account is a different session, not an update. Merging
            # across players would splice one account's token onto another's id.
            incoming = body.get("playerId")
            existing = creds.get("playerId")
            if incoming and existing and str(incoming) != str(existing):
                creds = {}
            updated = False
            for field in CREDENTIAL_FIELDS:
                value = body.get(field)
                if value is None or value == "":
                    continue  # never clobber a known value with a blank
                if creds.get(field) != value:
                    creds[field] = value
                    updated = True
            if not updated:
                return False
            creds["__captured__"] = datetime.now(timezone.utc).isoformat()
            creds["__warning__"] = WARNING
            self.path.write_text(json.dumps(creds, indent=2), encoding="utf-8")
        return True

    def _read_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (ValueError, OSError):
            return {}

    def is_complete(self) -> bool:
        """True when we have everything /GetDungeon needs.

        `platformVerificationId` is the one the server actually enforces:
        without it every request comes back 400 Bad Request.
        """
        creds = self.load() or {}
        return bool(creds.get("verificationToken")) and bool(
            creds.get("platformVerificationId")
        )

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (ValueError, OSError):
            return None
        return {k: v for k, v in data.items() if not k.startswith("__")}

    def captured_at(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text("utf-8")).get("__captured__")
        except (ValueError, OSError):
            return None
