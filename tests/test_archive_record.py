"""Archiving must record the request body, and must never break a response.

Regression: the asset-harvest block reassigned `body` -- the request-body
parameter -- to the parsed response JSON. `_archive` then received a dict where
it expected bytes and raised, so the server closed every capture-mode
connection without answering. The game just showed "connection error".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.upstream import Forwarder  # noqa: E402

REQUEST = {"userId": 0, "platform": "STEAM", "verificationToken": "secret-ticket"}
RESPONSE = {"dungeonSeed": -951959203, "layoutDownloadURL": "https://example/x"}


def _archive(tmp_path: Path, req_body) -> dict:
    fwd = Forwarder(tmp_path)
    fwd._archive(
        method="POST",
        url="https://gameservice.wiby.net/GetDungeon",
        req_headers={"Content-Type": "application/json"},
        req_body=req_body,
        status=200,
        resp_headers={},
        resp_body=json.dumps(RESPONSE).encode(),
    )
    lines = [
        l for l in (tmp_path / "GetDungeon.jsonl").read_text("utf-8").splitlines() if l.strip()
    ]
    return json.loads(lines[-1])


def test_request_body_is_recorded(tmp_path: Path) -> None:
    entry = _archive(tmp_path, json.dumps(REQUEST).encode())
    assert entry["request_body"]["userId"] == 0
    assert entry["request_body"]["platform"] == "STEAM"


def test_credential_still_redacted(tmp_path: Path) -> None:
    entry = _archive(tmp_path, json.dumps(REQUEST).encode())
    assert "secret-ticket" not in json.dumps(entry)


def test_empty_body_does_not_raise(tmp_path: Path) -> None:
    entry = _archive(tmp_path, b"")
    assert entry["status"] == 200


def test_non_bytes_body_does_not_raise(tmp_path: Path) -> None:
    """Archiving is best-effort; it must not take the response down with it."""
    for bad in (None, {"already": "parsed"}, "a string"):
        entry = _archive(tmp_path, bad)
        assert entry["status"] == 200
        assert entry["response_body"]["dungeonSeed"] == -951959203


def test_response_body_recorded(tmp_path: Path) -> None:
    entry = _archive(tmp_path, b"{}")
    assert entry["response_body"]["dungeonSeed"] == -951959203
