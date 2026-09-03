"""Engine.ini editing must never damage the player's real config.

Unreal .ini files repeat keys (`Paths=` ~25 times). An earlier version used
configparser here and silently collapsed them, which would have broken the
game's content paths -- hence these tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phantom_offline import client_config  # noqa: E402

REAL_WORLD_INI = """[Core.System]
Paths=../../../Engine/Content
Paths=%GAMEDIR%Content
Paths=../../../PhantomAbyss/Plugins/DungeonArchitect/Content
Paths=../../../Engine/Plugins/FX/Niagara/Content

[/Script/Engine.RendererSettings]
r.DefaultFeature.Bloom=True
+SomeArray=one
+SomeArray=two
"""


def _use_temp_ini(tmp_path: Path) -> Path:
    ini = tmp_path / "Engine.ini"
    client_config.APPDATA_CONFIG = tmp_path
    client_config.ENGINE_INI = ini
    return ini


def test_duplicate_keys_survive(tmp_path: Path) -> None:
    ini = _use_temp_ini(tmp_path)
    ini.write_text(REAL_WORLD_INI, encoding="utf-8")

    client_config.configure_proxy(54909)
    text = ini.read_text(encoding="utf-8")

    assert text.count("Paths=") == 4, "duplicate Paths= entries were collapsed"
    assert text.count("+SomeArray=") == 2
    assert "HttpProxyAddress=127.0.0.1:54909" in text
    assert "bVerifyPeer=False" in text


def test_restore_returns_original(tmp_path: Path) -> None:
    ini = _use_temp_ini(tmp_path)
    ini.write_text(REAL_WORLD_INI, encoding="utf-8")

    client_config.configure_proxy(54909)
    client_config.restore()

    after = ini.read_text(encoding="utf-8")
    assert after.strip() == REAL_WORLD_INI.strip(), "restore did not round-trip"
    assert "HttpProxyAddress" not in after
    assert "[HTTP]" not in after


def test_configure_is_idempotent(tmp_path: Path) -> None:
    ini = _use_temp_ini(tmp_path)
    ini.write_text(REAL_WORLD_INI, encoding="utf-8")

    client_config.configure_proxy(54909)
    once = ini.read_text(encoding="utf-8")
    client_config.configure_proxy(54909)
    twice = ini.read_text(encoding="utf-8")

    assert once == twice, "repeated configuration accumulated duplicate settings"
    assert twice.count("HttpProxyAddress") == 1


def test_configure_on_missing_file(tmp_path: Path) -> None:
    ini = _use_temp_ini(tmp_path)
    assert not ini.exists()

    client_config.configure_proxy(54909)
    text = ini.read_text(encoding="utf-8")
    assert "HttpProxyAddress=127.0.0.1:54909" in text


def test_port_change_replaces_not_appends(tmp_path: Path) -> None:
    ini = _use_temp_ini(tmp_path)
    ini.write_text(REAL_WORLD_INI, encoding="utf-8")

    client_config.configure_proxy(54909)
    client_config.configure_proxy(9999)
    text = ini.read_text(encoding="utf-8")

    assert text.count("HttpProxyAddress") == 1
    assert "9999" in text and "54909" not in text
