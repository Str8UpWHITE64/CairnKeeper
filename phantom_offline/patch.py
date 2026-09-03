"""Redirect the game's backend URLs to a local plain-HTTP server.

Why this exists
---------------
The clean approach is to intercept HTTPS with a generated certificate, but
Unreal's SSL module installs its own trust store through a
``CURLOPT_SSL_CTX_FUNCTION`` callback and re-verifies the peer even when
``[HTTP.Curl] bVerifyPeer=False`` is set. Rather than fight the TLS stack, this
rewrites the backend URLs in the executable so the game speaks plain HTTP to
``127.0.0.1`` and no certificate is ever involved.

The URLs are UTF-16LE string literals, rewritten in place so the file size and
every offset inside it are unchanged.

Padding, and why it matters
---------------------------
The first attempt NUL-terminated the shorter replacement and zeroed the rest.
That failed: the game requested ``/`` instead of ``/VerifyUserID``. The string
length is fixed at compile time rather than measured with strlen, so the base
URL kept its original length and ``base + "/VerifyUserID"`` became

    http://127.0.0.1:54908\0\0\0\0\0\0/VerifyUserID

which truncates at the first NUL on the way into libcurl -- leaving the bare
base URL and no endpoint path.

So the replacement is padded to *exactly* the original character count with a
throwaway path segment of underscores:

    https://gameservice.wiby.net   ->  http://127.0.0.1:54908/______

The game then requests ``/______/VerifyUserID``, and the server strips any
underscore-only leading segment (see ``server.strip_pad``). No NULs, no
truncation.

A backup is taken before the first write, and ``restore()`` puts it back.
Verifying the game files through Steam also restores the original.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

# Every backend URL baked into the shipping binary, per EServerTarget.
TARGET_URLS = (
    "https://redirector.wiby.net",
    "https://redirector.dev.wiby.net",
    "https://redirector.test.wiby.net",
    "https://redirector.beta.wiby.net",
    "https://gameservice.wiby.net",
    "https://gameservice.dev.wiby.net",
    "https://gameservice.test.wiby.net",
    "https://gameservice.beta.wiby.net",
    "https://gameservice.xboxsandbox.wiby.net",
)


@dataclass(frozen=True)
class Replacement:
    url: str
    offset: int
    original: str


PAD_CHAR = "_"


def _encode(text: str) -> bytes:
    return text.encode("utf-16-le")


def padded_url(local_url: str, target_length: int) -> str:
    """Extend *local_url* to exactly *target_length* characters.

    The padding is a path segment of underscores, which the server strips. The
    length must match the original exactly -- see the module docstring.
    """
    if len(local_url) == target_length:
        return local_url
    extra = target_length - len(local_url)
    if extra < 2:
        # Need at least "/" plus one padding character to stay a valid path.
        raise ValueError(
            f"cannot pad {local_url!r} ({len(local_url)}) to {target_length}: "
            "need at least 2 spare characters"
        )
    return local_url + "/" + PAD_CHAR * (extra - 1)


def find_urls(data: bytes, urls: tuple[str, ...] = TARGET_URLS) -> list[Replacement]:
    """Locate every UTF-16LE occurrence of the backend URLs."""
    found: list[Replacement] = []
    for url in urls:
        needle = _encode(url)
        start = 0
        while True:
            idx = data.find(needle, start)
            if idx == -1:
                break
            found.append(Replacement(url=url, offset=idx, original=url))
            start = idx + 2
    return sorted(found, key=lambda r: r.offset)


def build_patch(data: bytes, local_url: str) -> tuple[bytearray, list[Replacement]]:
    """Return patched bytes plus the list of sites rewritten.

    Raises ValueError if the replacement will not fit, rather than corrupting
    neighbouring data.
    """
    out = bytearray(data)
    sites = find_urls(data)

    for site in sites:
        original = _encode(site.original)
        if len(local_url) > len(site.original):
            raise ValueError(
                f"replacement {local_url!r} ({len(local_url)} chars) does not fit "
                f"in {site.original!r} ({len(site.original)} chars)"
            )
        # Exact-length replacement: no NULs, so nothing truncates downstream.
        replacement = _encode(padded_url(local_url, len(site.original)))
        assert len(replacement) == len(original)
        out[site.offset : site.offset + len(original)] = replacement

    return out, sites


def backup_path(exe: Path, backup_dir: Path) -> Path:
    return backup_dir / f"{exe.name}.original"


def is_patched(exe: Path) -> bool:
    return not find_urls(exe.read_bytes())


def apply(
    exe: Path, *, backup_dir: Path, port: int, dry_run: bool = False
) -> tuple[int, Path | None]:
    """Patch *exe* to talk to 127.0.0.1:*port* over plain HTTP.

    Returns (number of sites rewritten, backup path).
    """
    data = exe.read_bytes()
    local_url = f"http://127.0.0.1:{port}"
    patched, sites = build_patch(data, local_url)

    if not sites:
        return 0, None
    if dry_run:
        return len(sites), None

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_path(exe, backup_dir)
    if not backup.exists():
        shutil.copy2(exe, backup)

    exe.write_bytes(bytes(patched))

    # Confirm the write actually took effect before reporting success.
    if find_urls(exe.read_bytes()):
        raise RuntimeError("patch verification failed: original URLs still present")
    return len(sites), backup


def restore(exe: Path, *, backup_dir: Path) -> bool:
    backup = backup_path(exe, backup_dir)
    if not backup.exists():
        return False
    shutil.copy2(backup, exe)
    return True
