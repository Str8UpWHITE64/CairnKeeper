"""Patched clients address us on localhost; capture mode must not loop back."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline.server import upstream_url  # noqa: E402


def test_redirector_path_maps_to_redirector() -> None:
    assert (
        upstream_url("http://127.0.0.1:54908/redirectV3.json")
        == "https://redirector.wiby.net/redirectV3.json"
    )


def test_other_paths_map_to_gameservice() -> None:
    assert (
        upstream_url("http://127.0.0.1:54908/GetDungeon")
        == "https://gameservice.wiby.net/GetDungeon"
    )
    assert (
        upstream_url("http://localhost:54908/WakeUp")
        == "https://gameservice.wiby.net/WakeUp"
    )


def test_never_forwards_to_itself() -> None:
    for path in ("/redirectV3.json", "/WakeUp", "/GetDungeon", "/SubmitRun"):
        assert "127.0.0.1" not in upstream_url(f"http://127.0.0.1:54908{path}")


def test_query_string_preserved() -> None:
    assert upstream_url("http://127.0.0.1:54908/GetDungeon?a=1&b=2").endswith(
        "/GetDungeon?a=1&b=2"
    )


def test_real_hostnames_pass_through_unchanged() -> None:
    url = "https://gameservice.wiby.net/WakeUp"
    assert upstream_url(url) == url
