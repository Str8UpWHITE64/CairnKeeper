"""Keep credentials out of archived captures.

The client's verification request carries a live Steam authentication ticket and
the player's SteamID64. Captures are the whole point of this project and are
likely to be shared, so credentials are stripped before anything reaches disk.

Redaction is deliberately applied at write time rather than on read: once a
secret is on disk it is effectively leaked, so it must never get there.
"""
from __future__ import annotations

import re
from typing import Any

# Live credentials. Never archive these.
SECRET_KEYS = frozenset(
    {
        "verificationtoken",
        "platformverificationid",
        "authtoken",
        "sessiontoken",
        "accesstoken",
        "refreshtoken",
        "ticket",
        "authticket",
        "password",
        "secret",
        "apikey",
    }
)

# Identifying but not secret. Replaced with a pseudonym so protocol structure
# stays readable.
IDENTITY_KEYS = frozenset({"playerid", "platformid", "userid", "platformuserid"})

# Keys whose values are platform IDs belonging to OTHER people -- friend lists,
# ghost owners, relic collectors. A shared capture must not become a list of
# third parties' Steam accounts.
IDENTITY_LIST_KEYS = frozenset(
    {
        "platformfriends",
        "friendplatformids",
        "ghostids",
        "relicollectorids",
        "reliccollectorids",
        "reliccollectoruserids",
        "sharerid",
        "shareruserid",
        "completedbyid",
        "userstoadd",
        "usertoadd",
    }
)

REDACTED = "<redacted>"
PSEUDONYM = "<player-id>"

# A SteamID64 is 7656119 followed by ten digits. Matched against keys as well
# as values, because a map can be keyed by one.
_LOOKS_LIKE_A_PLATFORM_ID = re.compile(r"^7656119\d{10}$")


def scrub(value: Any, *, keep_identity: bool = False) -> Any:
    """Recursively replace credential values. Structure is preserved.

    Identifiers hide in keys as well as values. `playerStats` carries
    `NumTimesRunWithPlayer`, a map keyed by the SteamID64 of everyone the
    player has run alongside -- a social graph of third parties sitting in a
    field name, where scrubbing by key name never looked. Nothing here reads
    those ids, and this module exists so that a shared capture does not become
    a list of other people's accounts.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not keep_identity and _LOOKS_LIKE_A_PLATFORM_ID.match(str(key)):
                out[PSEUDONYM] = scrub(item, keep_identity=keep_identity)
                continue
            lowered = str(key).lower()
            if lowered in SECRET_KEYS and _is_present(item):
                out[key] = REDACTED
            elif keep_identity:
                out[key] = scrub(item, keep_identity=True)
            elif lowered in IDENTITY_KEYS and isinstance(item, str) and item:
                out[key] = PSEUDONYM
            elif lowered in IDENTITY_LIST_KEYS:
                out[key] = _pseudonymise(item)
            else:
                out[key] = scrub(item, keep_identity=keep_identity)
        return out
    if isinstance(value, list):
        return [scrub(v, keep_identity=keep_identity) for v in value]
    return value


def _pseudonymise(value: Any) -> Any:
    """Replace an id, or a list of ids, while preserving shape and count."""
    if isinstance(value, str):
        return PSEUDONYM if value else value
    if isinstance(value, list):
        return [_pseudonymise(v) for v in value]
    return value


def _is_present(value: Any) -> bool:
    """Only redact values that actually carry something."""
    return not (value is None or value == "" or value == 0)


def scrub_headers(headers: dict[str, str]) -> dict[str, str]:
    sensitive = {"authorization", "cookie", "set-cookie", "x-api-key"}
    return {
        k: (REDACTED if k.lower() in sensitive else v) for k, v in headers.items()
    }
