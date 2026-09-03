"""Never launch the unshipped Development build.

The install contains a 179 MB `PhantomAbyss/Binaries/Win64/PhantomAbyss.exe`
that is absent from the game's own file manifest. Running it against the
Shipping-cooked .pak crashes in FAsyncLoadingThread with
"Could not find SuperStruct FellOutOfWorld to create FellOutOfWorld".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import client_config  # noqa: E402

DEV_BUILD = Path("PhantomAbyss") / "Binaries" / "Win64" / "PhantomAbyss.exe"


def _make_install(root: Path, *relatives: Path) -> Path:
    for rel in relatives:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"stub")
    return root


def test_dev_build_is_never_a_candidate() -> None:
    assert DEV_BUILD not in client_config.EXE_CANDIDATES


def test_prefers_shipping_exe(tmp_path: Path) -> None:
    install = _make_install(
        tmp_path,
        client_config.SHIPPING_EXE,
        client_config.LAUNCHER_EXE,
        DEV_BUILD,
    )
    assert client_config.find_exe(install) == install / client_config.SHIPPING_EXE


def test_falls_back_to_root_launcher(tmp_path: Path) -> None:
    install = _make_install(tmp_path, client_config.LAUNCHER_EXE, DEV_BUILD)
    assert client_config.find_exe(install) == install / client_config.LAUNCHER_EXE


def test_dev_build_alone_is_not_launchable(tmp_path: Path) -> None:
    install = _make_install(tmp_path, DEV_BUILD)
    assert client_config.find_exe(install) is None


def test_find_game_ignores_install_with_only_dev_build(
    tmp_path: Path, monkeypatch
) -> None:
    install = _make_install(tmp_path, DEV_BUILD)
    monkeypatch.setattr(client_config, "KNOWN_GAME_PATHS", (install,))
    assert client_config.find_game() is None
