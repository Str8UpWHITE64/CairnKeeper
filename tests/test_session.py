"""Credential capture must accumulate, never downgrade.

Regression: the client sends a partial set at /WakeUp (token present,
platformVerificationId empty). Overwriting a complete set with that partial one
left the archiver with unusable credentials, and every /GetDungeon came back
400 Bad Request.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.session import SessionStore  # noqa: E402

FULL = {
    "userId": 62842,
    "playerId": "76561198000000042",
    "platform": "STEAM",
    "verificationToken": "TICKET",
    "platformVerificationId": "PVID",
    "clientDungeonVersion": 128,
    "clientProtocolVersion": 7,
}
PARTIAL_WAKEUP = {
    "userId": 0,
    "playerId": "",
    "platform": "STEAM",
    "verificationToken": "TICKET2",
    "platformVerificationId": "",
    "clientDungeonVersion": 128,
    "clientProtocolVersion": 7,
}


def test_records_full_credentials(tmp_path: Path) -> None:
    s = SessionStore(tmp_path)
    assert s.remember(FULL)
    assert s.load()["platformVerificationId"] == "PVID"
    assert s.is_complete()


def test_partial_does_not_erase_verification_id(tmp_path: Path) -> None:
    s = SessionStore(tmp_path)
    s.remember(FULL)
    s.remember(PARTIAL_WAKEUP)
    creds = s.load()
    assert creds["platformVerificationId"] == "PVID", "blank overwrote a real value"
    assert creds["verificationToken"] == "TICKET2", "fresher token should win"
    assert s.is_complete()


def test_partial_alone_is_incomplete(tmp_path: Path) -> None:
    s = SessionStore(tmp_path)
    s.remember(PARTIAL_WAKEUP)
    assert not s.is_complete()


def test_request_without_token_ignored(tmp_path: Path) -> None:
    s = SessionStore(tmp_path)
    assert not s.remember({"userId": 1, "verificationToken": ""})
    assert s.load() is None


def test_load_strips_metadata(tmp_path: Path) -> None:
    s = SessionStore(tmp_path)
    s.remember(FULL)
    assert not any(k.startswith("__") for k in s.load())
    assert s.captured_at() is not None


def test_no_credentials_is_not_complete(tmp_path: Path) -> None:
    assert not SessionStore(tmp_path).is_complete()


def test_different_account_resets_credentials(tmp_path: Path) -> None:
    """A second player is a new session, not an update to the first."""
    s = SessionStore(tmp_path)
    s.remember(FULL)
    s.remember(
        {
            "userId": 0,
            "playerId": "76561198000000044",
            "platform": "STEAM",
            "verificationToken": "OTHER-TICKET",
            "platformVerificationId": "OTHER-PVID",
            "clientDungeonVersion": 128,
            "clientProtocolVersion": 7,
        }
    )
    creds = s.load()
    assert creds["playerId"] == "76561198000000044"
    assert creds["verificationToken"] == "OTHER-TICKET"
    assert creds["platformVerificationId"] == "OTHER-PVID", "must not keep the old id"


def test_same_account_still_merges(tmp_path: Path) -> None:
    s = SessionStore(tmp_path)
    s.remember(FULL)
    s.remember({**PARTIAL_WAKEUP, "playerId": FULL["playerId"]})
    assert s.load()["platformVerificationId"] == "PVID"
