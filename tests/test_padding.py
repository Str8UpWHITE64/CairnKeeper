"""Patched URLs must be exactly the original length, and pad must round-trip.

Regression: NUL-padding a shorter URL made the game request `/` instead of
`/VerifyUserID`, because the string length is fixed at compile time and the
embedded NULs truncated `base + endpoint`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import patch  # noqa: E402
from phantom_offline.server import strip_pad  # noqa: E402

LOCAL = "http://127.0.0.1:54908"


@pytest.mark.parametrize("url", patch.TARGET_URLS)
def test_padded_url_matches_original_length(url: str) -> None:
    assert len(patch.padded_url(LOCAL, len(url))) == len(url)


@pytest.mark.parametrize("url", patch.TARGET_URLS)
def test_padding_round_trips_through_strip(url: str) -> None:
    padded = patch.padded_url(LOCAL, len(url))
    path = padded[len("http://127.0.0.1:54908") :]
    # The game appends the endpoint to the padded base.
    assert strip_pad(f"{path}/VerifyUserID") == "/VerifyUserID"


def test_patched_bytes_contain_no_nuls() -> None:
    """Embedded NULs are exactly what broke the first attempt."""
    blob = b"\x00" * 8 + "https://gameservice.wiby.net".encode("utf-16-le") + b"\xff" * 8
    out, sites = patch.build_patch(blob, LOCAL)
    site = sites[0]
    region = bytes(out[site.offset : site.offset + len(site.original) * 2])
    assert b"\x00\x00" not in region, "NUL terminator would truncate the URL"
    assert region.decode("utf-16-le").startswith(LOCAL)


def test_bare_padded_path_reduces_to_root() -> None:
    assert strip_pad("/______") == "/"
    assert strip_pad("/") == "/"


def test_strip_pad_leaves_real_paths_alone() -> None:
    assert strip_pad("/VerifyUserID") == "/VerifyUserID"
    assert strip_pad("/redirectV3.json") == "/redirectV3.json"
    # An underscore inside a real segment must not be mistaken for padding.
    assert strip_pad("/Get_Dungeon") == "/Get_Dungeon"


def test_refuses_when_too_little_room_to_pad() -> None:
    with pytest.raises(ValueError, match="at least 2 spare"):
        patch.padded_url(LOCAL, len(LOCAL) + 1)


def test_exact_fit_needs_no_padding() -> None:
    assert patch.padded_url(LOCAL, len(LOCAL)) == LOCAL
