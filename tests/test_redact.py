"""Credentials must never reach disk.

The body below is the real verification request captured from the game; the
token is a live Steam authentication ticket.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.redact import PSEUDONYM, REDACTED, scrub, scrub_headers  # noqa: E402

REAL_REQUEST = {
    "userId": 0,
    "playerId": "76561198000000042",
    "platform": "STEAM",
    "verificationToken": "STEAM-AUTH-TICKET-PLACEHOLDER-NOT-A-REAL-ONE",
    "platformVerificationId": "",
    "clientDungeonVersion": 128,
    "clientProtocolVersion": 7,
}


def test_token_is_removed() -> None:
    out = scrub(REAL_REQUEST)
    assert out["verificationToken"] == REDACTED
    assert "CAIQ" not in json.dumps(out)


def test_steam_id_is_pseudonymised() -> None:
    out = scrub(REAL_REQUEST)
    assert out["playerId"] == PSEUDONYM
    assert "76561198000000042" not in json.dumps(out)


def test_protocol_fields_survive() -> None:
    """Redaction must not destroy the data we are trying to preserve."""
    out = scrub(REAL_REQUEST)
    assert out["clientDungeonVersion"] == 128
    assert out["clientProtocolVersion"] == 7
    assert out["platform"] == "STEAM"
    assert set(out) == set(REAL_REQUEST), "field structure changed"


def test_empty_values_not_falsely_redacted() -> None:
    out = scrub(REAL_REQUEST)
    assert out["platformVerificationId"] == ""
    assert out["userId"] == 0


def test_nested_and_listed_secrets() -> None:
    payload = {
        "ghostRuns": [
            {"runID": "a", "verificationToken": "secret1"},
            {"runID": "b", "authToken": "secret2"},
        ],
        "inner": {"deep": {"password": "hunter2"}},
    }
    dumped = json.dumps(scrub(payload))
    for secret in ("secret1", "secret2", "hunter2"):
        assert secret not in dumped


def test_keep_identity_option() -> None:
    out = scrub(REAL_REQUEST, keep_identity=True)
    assert out["playerId"] == "76561198000000042"
    assert out["verificationToken"] == REDACTED, "tokens redact regardless"


def test_header_scrubbing() -> None:
    out = scrub_headers({"Authorization": "Bearer xyz", "Content-Type": "application/json"})
    assert out["Authorization"] == REDACTED
    assert out["Content-Type"] == "application/json"


def test_scrub_passes_through_scalars() -> None:
    assert scrub("plain") == "plain"
    assert scrub(None) is None
    assert scrub(42) == 42


def test_an_identifier_used_as_a_key_is_scrubbed_too(tmp_path=None) -> None:
    """The social graph hid in field names, where scrubbing never looked.

    `playerStats.NumTimesRunWithPlayer` is keyed by the SteamID64 of everyone
    the player has run alongside. Scrubbing by key name checked the values, so
    a map of other people's accounts went to disk untouched -- found in a real
    captured login after this had been running for weeks.
    """
    captured = {
        "playerStats": {
            "TotalDistanceTravelled": 15405592.0,
            "NumTimesRunWithPlayer": {
                "76561198000000777": 3,
                "76561198000000123": 1,
            },
        }
    }
    cleaned = scrub(captured)
    graph = cleaned["playerStats"]["NumTimesRunWithPlayer"]
    assert "76561198000000777" not in graph
    assert "76561198000000123" not in graph
    assert "76561198000000777" not in json.dumps(cleaned)
    assert cleaned["playerStats"]["TotalDistanceTravelled"] == 15405592.0


def test_keeping_identity_keeps_the_keys(tmp_path=None) -> None:
    """The player's own copy is theirs; only what leaves is scrubbed."""
    captured = {"playerStats": {"NumTimesRunWithPlayer": {"76561198000000777": 3}}}
    kept = scrub(captured, keep_identity=True)
    assert kept["playerStats"]["NumTimesRunWithPlayer"] == {"76561198000000777": 3}
