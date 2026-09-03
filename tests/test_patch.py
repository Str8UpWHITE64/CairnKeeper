"""URL patching must be size-preserving and exactly reversible."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import patch  # noqa: E402

LOCAL = "http://127.0.0.1:54908"


def _blob() -> bytes:
    """A stand-in binary with the URLs embedded as UTF-16LE, like the real exe."""
    parts = [b"\x00" * 32]
    for url in ("https://redirector.wiby.net", "https://gameservice.wiby.net"):
        parts.append(url.encode("utf-16-le") + b"\x00\x00")
        parts.append(b"\xab" * 16)
    return b"".join(parts)


def test_finds_every_url() -> None:
    sites = patch.find_urls(_blob())
    assert {s.url for s in sites} == {
        "https://redirector.wiby.net",
        "https://gameservice.wiby.net",
    }


def test_patch_preserves_size() -> None:
    data = _blob()
    out, sites = patch.build_patch(data, LOCAL)
    assert len(out) == len(data), "patch changed the file size"
    assert len(sites) == 2


def test_patched_binary_has_no_original_urls() -> None:
    out, _ = patch.build_patch(_blob(), LOCAL)
    assert not patch.find_urls(bytes(out))


def test_replacement_is_readable_and_exactly_fills_the_slot() -> None:
    """No NUL terminator: the slot is filled to its exact original length.

    See tests/test_padding.py -- NUL padding truncated `base + endpoint`.
    """
    out, sites = patch.build_patch(_blob(), LOCAL)
    for site in sites:
        raw = bytes(out[site.offset : site.offset + len(site.original) * 2])
        text = raw.decode("utf-16-le")
        assert len(text) == len(site.original)
        assert text.startswith(LOCAL)
        assert "\x00" not in text


def test_surrounding_bytes_untouched() -> None:
    data = _blob()
    out, _ = patch.build_patch(data, LOCAL)
    # The 0xAB filler between strings must survive verbatim.
    assert bytes(out).count(b"\xab" * 16) == data.count(b"\xab" * 16)
    assert bytes(out[:32]) == data[:32]


def test_oversized_replacement_refuses() -> None:
    with pytest.raises(ValueError, match="does not fit"):
        patch.build_patch(_blob(), "http://a-really-long-hostname.example.test:54908")


def test_no_sites_is_not_an_error() -> None:
    out, sites = patch.build_patch(b"\x00" * 64, LOCAL)
    assert sites == []
    assert bytes(out) == b"\x00" * 64
