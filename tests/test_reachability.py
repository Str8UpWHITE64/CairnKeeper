"""Never tell a player their game is dead when their wifi dropped.

The whole project exists because the service will one day go away, so the
client has to notice. It also has to be honest about what it cannot tell: from
one machine, a service that shut down forever looks exactly like one that is up
while the connection is down. That ambiguity is permanent, so the client never
claims the former.
"""
from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phantom_offline.reachability as reach  # noqa: E402

# The real body /WakeUp answers with, captured from the live service.
LIVE_BODY = json.dumps({
    "mode": 1, "newGamesLockedOut": False,
    "maintenanceTimeUTC": "2026-08-27T15:50:48.934Z",
    "serverVersion": 128, "serverProtocolVersion": 7,
}).encode()


class _Response:
    # close() is not decoration: HTTPError takes this as its file object and
    # closes it when collected, which without one raised out of __del__ and
    # left an unraisable-exception warning on an otherwise clean run.
    def __init__(self, body): self._body = body
    def read(self, n=None): return self._body
    def close(self): return None
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_a_healthy_service_is_recognised(monkeypatch) -> None:
    monkeypatch.setattr(reach.urllib.request, "urlopen",
                        lambda *a, **k: _Response(LIVE_BODY))
    got = reach.check()
    assert got.state == reach.HEALTHY
    assert got.usable


def test_a_503_carrying_a_maintenance_body_is_not_a_failure(monkeypatch) -> None:
    """503 was misread as "the servers are dead" once already.

    The real service answers /WakeUp with 503 and a full maintenance body while
    working perfectly well.
    """
    def raise_503(*a, **k):
        raise urllib.error.HTTPError("u", 503, "Service Unavailable", {},
                                     _Response(LIVE_BODY))
    monkeypatch.setattr(reach.urllib.request, "urlopen", raise_503)
    got = reach.check()
    assert got.state == reach.MAINTENANCE
    assert got.usable, "maintenance is the service working, not failing"


def test_nothing_answering_is_never_called_dead(monkeypatch) -> None:
    """The message must survive being wrong, because usually it is."""
    def refuse(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(reach.urllib.request, "urlopen", refuse)
    got = reach.check()
    assert got.state == reach.UNREACHABLE
    assert not got.usable
    text = got.message().lower()
    assert "can't reach" in text
    for claim in ("shut down", "gone", "dead", "forever", "permanently"):
        assert claim not in text, f"claimed more than we know: {claim!r}"


def test_the_offline_route_is_always_offered(monkeypatch) -> None:
    def refuse(*a, **k):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(reach.urllib.request, "urlopen", refuse)
    assert "offline" in reach.check().message().lower()


def test_an_unrecognised_reply_is_not_mistaken_for_health(monkeypatch) -> None:
    """A captive portal answers everything with 200 and an HTML page."""
    monkeypatch.setattr(reach.urllib.request, "urlopen",
                        lambda *a, **k: _Response(b"<html>sign in</html>"))
    got = reach.check()
    assert got.state == reach.REFUSED
    assert not got.usable


def test_locked_out_new_games_reads_as_maintenance(monkeypatch) -> None:
    body = json.dumps({"mode": 1, "newGamesLockedOut": True,
                       "serverVersion": 128}).encode()
    monkeypatch.setattr(reach.urllib.request, "urlopen",
                        lambda *a, **k: _Response(body))
    assert reach.check().state == reach.MAINTENANCE
