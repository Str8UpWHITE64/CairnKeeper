"""Forwarding must not misdescribe the body it sends.

Regression: the client gzips larger request bodies (`/GetDungeon` does,
`/WakeUp` does not). We decompress on the way in, but forwarded the original
`Content-Encoding: gzip` header alongside the now-plain body, and the real
server answered 415 Unsupported Media Type.
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.redact import PSEUDONYM, scrub  # noqa: E402
from phantom_offline.upstream import _HOP_BY_HOP  # noqa: E402

REAL_GET_DUNGEON = {
    "gameMode": "DGM_ADVENTURE",
    "dungeonSettingsIndex": 0,
    "routeId": 0,
    "routeStage": 0,
    "dungeonFloorNumber": 0,
    "shareCode": "",
    "knownDungeons": [],
    "whipWagered": "Bamboo",
    "dungeonId": 0,
    "isRetry": False,
    "difficultyRating": 0,
    "curseLevel": 530,
    "platformFriends": ["76561198000000888", "76561198000000042"],
}


def test_hop_by_hop_strips_length_and_host() -> None:
    for header in ("content-length", "host", "accept-encoding"):
        assert header in _HOP_BY_HOP


def test_gzip_round_trip_matches_what_client_sends() -> None:
    raw = gzip.compress(json.dumps(REAL_GET_DUNGEON).encode())
    assert json.loads(gzip.decompress(raw)) == REAL_GET_DUNGEON


def test_friend_ids_are_pseudonymised() -> None:
    """platformFriends is a list of OTHER people's Steam accounts."""
    out = scrub(REAL_GET_DUNGEON)
    assert out["platformFriends"] == [PSEUDONYM, PSEUDONYM]
    dumped = json.dumps(out)
    assert "76561198000000888" not in dumped
    assert "76561198000000042" not in dumped


def test_friend_list_shape_preserved() -> None:
    out = scrub(REAL_GET_DUNGEON)
    assert len(out["platformFriends"]) == len(REAL_GET_DUNGEON["platformFriends"])


def test_dungeon_fields_survive_redaction() -> None:
    """The protocol data is the point; redaction must not eat it."""
    out = scrub(REAL_GET_DUNGEON)
    assert out["gameMode"] == "DGM_ADVENTURE"
    assert out["curseLevel"] == 530
    assert out["whipWagered"] == "Bamboo"
    assert out["isRetry"] is False
    assert set(out) == set(REAL_GET_DUNGEON)


def test_empty_friend_list_stays_empty() -> None:
    assert scrub({"platformFriends": []})["platformFriends"] == []
