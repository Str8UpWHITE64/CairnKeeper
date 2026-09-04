"""The client must stay pure standard library on the path players use.

Not for elegance. A frozen interpreter that patches another program's
executable already looks like a trojan to a virus scanner, and every compiled
library bundled alongside it is one more thing to weigh. Keeping the play path
dependency-free keeps the download small and the false-positive risk lower.

The one third-party package is `cryptography`, needed only by the TLS proxy --
a fallback for an unpatched game, which the supervisor never leaves it as.
"""
from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PLAY_PATH = [
    "phantom_offline.server",
    "phantom_offline.supervisor",
    "phantom_offline.backend",
    "phantom_offline.library",
    "phantom_offline.profile",
    "phantom_offline.patch",
    "phantom_offline.bundles",
    "phantom_offline.fidelity",
    "phantom_offline.reachability",
    "phantom_offline.blacklist",
]

THIRD_PARTY = {"cryptography", "requests", "numpy", "pydantic", "httpx"}


def test_nothing_third_party_on_the_play_path() -> None:
    seen: set[str] = set()
    real = builtins.__import__

    def watch(name, *args, **kwargs):
        root = name.split(".")[0]
        if root in THIRD_PARTY:
            seen.add(root)
        return real(name, *args, **kwargs)

    for module in PLAY_PATH:
        sys.modules.pop(module, None)

    builtins.__import__ = watch
    try:
        for module in PLAY_PATH:
            importlib.import_module(module)
    finally:
        builtins.__import__ = real

    assert not seen, f"the play path pulled in {sorted(seen)}"


def test_certificates_are_only_loaded_when_wanted() -> None:
    """Importing the cert store must not drag the toolkit in with it."""
    sys.modules.pop("phantom_offline.certs", None)
    seen: set[str] = set()
    real = builtins.__import__

    def watch(name, *args, **kwargs):
        if name.split(".")[0] == "cryptography":
            seen.add("cryptography")
        return real(name, *args, **kwargs)

    builtins.__import__ = watch
    try:
        importlib.import_module("phantom_offline.certs")
    finally:
        builtins.__import__ = real

    assert not seen, "importing certs.py loaded the crypto toolkit"


def test_the_project_declares_no_required_dependencies() -> None:
    """A promise worth keeping honest: the download needs nothing extra."""
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text("utf-8")
    block = text.split("dependencies = ", 1)[1].splitlines()[0]
    assert block.strip() == "[]", f"required dependencies crept in: {block}"


def test_the_build_script_excludes_what_it_claims_to() -> None:
    import build  # noqa: PLC0415

    assert "cryptography" in build.EXCLUDE
    for heavy in ("numpy", "pandas", "matplotlib"):
        assert heavy in build.EXCLUDE


def test_a_frozen_build_does_not_store_anything_in_a_temporary_folder(
    monkeypatch, tmp_path
) -> None:
    """PyInstaller unpacks into a directory it deletes on exit.

    Writing the archive beside the program would lose every temple, phantom and
    profile the moment the player closed the game -- the one outcome this whole
    project exists to prevent.
    """
    import phantom_offline.__main__ as entry

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("PHANTOM_OFFLINE_STATE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(entry, "ARCHIVE_POINTER", tmp_path / "nope")

    resolved = entry._default_state()
    # Somewhere durable the player chose, not beside the unpacked program.
    assert resolved == tmp_path / "AppData" / "CairnKeeper"
    assert "_MEI" not in str(resolved), "landed in PyInstaller's scratch folder"
    assert entry.PROJECT_ROOT not in resolved.parents


def test_an_explicit_state_directory_still_wins_when_frozen(
    monkeypatch, tmp_path
) -> None:
    import phantom_offline.__main__ as entry

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("PHANTOM_OFFLINE_STATE", str(tmp_path / "chosen"))
    assert entry._default_state() == tmp_path / "chosen"


def _without_cryptography():
    """A context where importing the certificate toolkit fails, as when frozen."""
    import contextlib

    @contextlib.contextmanager
    def guard():
        real = builtins.__import__

        def watch(name, *args, **kwargs):
            if name.split(".")[0] == "cryptography":
                raise ImportError("No module named 'cryptography'")
            return real(name, *args, **kwargs)

        builtins.__import__ = watch
        try:
            yield
        finally:
            builtins.__import__ = real

    return guard()


def test_a_server_starts_without_the_certificate_toolkit(tmp_path) -> None:
    """Importing cleanly is not the same as running.

    The earlier test only checked imports, so it passed while the packaged
    build died at "Starting the local server" -- the certificate store built
    its authority eagerly, and the offline server never terminates TLS at all.
    """
    from phantom_offline import server

    with _without_cryptography():
        for mode in ("offline", "capture"):
            direct, proxy, _ = server.build(
                state_dir=tmp_path / mode,
                direct_port=54960, proxy_port=54961, mode=mode,
            )
            try:
                assert direct is not None and proxy is not None
            finally:
                # Both rounds want the same ports. Windows allowed the second
                # bind anyway and hid this; Linux refused it.
                direct.server_close()
                proxy.server_close()


def test_certificates_are_still_made_when_something_wants_one(tmp_path) -> None:
    """Deferring must not mean never."""
    from phantom_offline.certs import CertStore

    store = CertStore(tmp_path / "certs")
    cert, key = store.cert_for("gameservice.wiby.net")
    assert Path(cert).exists() and Path(key).exists()
    assert Path(store.ca_cert_path).exists(), "asking for the path should make it"


def test_an_archive_left_by_the_older_name_is_still_found(
    tmp_path: Path, monkeypatch
) -> None:
    """The project was called something else before it was named.

    Anybody who ran an early build has an archive in a folder under the old
    name. A rename that silently walked past somebody's temples and recordings
    and started a fresh empty one would be a poor introduction.
    """
    from phantom_offline.__main__ import _app_home

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert _app_home().name == "CairnKeeper", "the name it has now"

    (tmp_path / "PhantomAbyssOffline").mkdir()
    assert _app_home().name == "PhantomAbyssOffline", "an archive already there"

    (tmp_path / "CairnKeeper").mkdir()
    assert _app_home().name == "CairnKeeper", "once it has one of its own"
