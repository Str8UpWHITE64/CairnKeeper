"""Points the game at our proxy without modifying any game file.

Everything here touches only the user-writable config under
``%LOCALAPPDATA%\\PhantomAbyss\\Saved``. The game installation, its ``.pak`` and
its executable are never altered, so the change is trivially reversible and
Steam file verification stays clean.

Two settings do the work:

* ``[HTTP] HttpProxyAddress``  -- routes libcurl through our proxy.
* ``[HTTP.Curl] bVerifyPeer``  -- lets the generated MITM certificate be accepted.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

APPDATA_CONFIG = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "PhantomAbyss"
    / "Saved"
    / "Config"
    / "WindowsNoEditor"
)
ENGINE_INI = APPDATA_CONFIG / "Engine.ini"

STEAM_APPID = "989440"
KNOWN_GAME_PATHS = (
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Phantom Abyss"),
    Path(r"D:\SteamLibrary\steamapps\common\Phantom Abyss"),
    Path(r"E:\SteamLibrary\steamapps\common\Phantom Abyss"),
)
# Launch candidates, best first.
#
# Only these two are in the game's own Manifest_NonUFSFiles_Win64.txt. There is
# also a 179 MB `PhantomAbyss/Binaries/Win64/PhantomAbyss.exe` in the install
# which is NOT in the manifest -- it is a leftover *Development* build. Running
# it against the Shipping-cooked .pak dies during async loading with
# "Could not find SuperStruct <X> to create <X>", so never launch it.
SHIPPING_EXE = Path("PhantomAbyss") / "Binaries" / "Win64" / "PhantomAbyss-Win64-Shipping.exe"
LAUNCHER_EXE = Path("PhantomAbyss.exe")
EXE_CANDIDATES = (SHIPPING_EXE, LAUNCHER_EXE)

# Present next to the shipping exe; lets the Steam API identify the app when the
# game is started directly rather than through the Steam client.
STEAM_APPID_FILE = Path("PhantomAbyss") / "Binaries" / "Win64" / "steam_appid.txt"


def find_game() -> Path | None:
    for base in KNOWN_GAME_PATHS:
        if any((base / rel).exists() for rel in EXE_CANDIDATES):
            return base
    return None


def _is_patched(game: Path) -> bool:
    """True when the shipping exe's backend URLs already point at localhost."""
    from . import patch

    exe = game / SHIPPING_EXE
    if not exe.exists():
        return False
    try:
        return not patch.find_urls(exe.read_bytes())
    except OSError:
        return False


def find_exe(game: Path) -> Path | None:
    for rel in EXE_CANDIDATES:
        candidate = game / rel
        if candidate.exists():
            return candidate
    return None


def backup_engine_ini() -> Path | None:
    if not ENGINE_INI.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = ENGINE_INI.with_suffix(f".ini.bak-{stamp}")
    shutil.copy2(ENGINE_INI, dest)
    return dest


MARKER = "; --- PhantomAbyss-Offline ---"

# Unreal .ini files legitimately repeat the same key (`Paths=` appears ~25 times
# in Engine.ini) and use +/-/. key prefixes. configparser silently collapses
# duplicates, which would destroy the game's content paths, so all editing here
# is line-based.
_OUR_KEYS = {
    "HTTP": ("HttpProxyAddress",),
    "HTTP.Curl": ("bVerifyPeer",),
    # Unreal's SSL module installs its own trust store through a
    # CURLOPT_SSL_CTX_FUNCTION callback, which re-enables peer verification
    # even when [HTTP.Curl] bVerifyPeer=False. These are the knobs that
    # actually reach that code path.
    "SSL": (
        "bValidateRootCertificates",
        "DebuggingCertificatePath",
        "bUsePlatformProvidedCertificates",
    ),
    "ConsoleVariables": ("n.VerifyPeer",),
}
_OUR_SECTIONS = tuple(f"[{name}]" for name in _OUR_KEYS)


def _read_lines() -> list[str]:
    if not ENGINE_INI.exists():
        return []
    text = ENGINE_INI.read_text(encoding="utf-8-sig")
    return text.splitlines()


def _write_lines(lines: list[str]) -> None:
    APPDATA_CONFIG.mkdir(parents=True, exist_ok=True)
    ENGINE_INI.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def _strip_our_settings(lines: list[str]) -> list[str]:
    """Drop only the keys we own, and any section we created that is now empty."""
    out: list[str] = []
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped == MARKER:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if section in _OUR_KEYS and key in _OUR_KEYS[section]:
            continue
        out.append(line)

    # Remove any section header we added that is now empty.
    cleaned: list[str] = []
    for idx, line in enumerate(out):
        stripped = line.strip()
        if stripped in _OUR_SECTIONS:
            rest = out[idx + 1 :]
            has_content = False
            for nxt in rest:
                n = nxt.strip()
                if n.startswith("[") and n.endswith("]"):
                    break
                if n and not n.startswith(";"):
                    has_content = True
                    break
            if not has_content:
                continue
        cleaned.append(line)

    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return cleaned


def configure_proxy(proxy_port: int, ca_cert: Path | None = None) -> Path:
    """Add our proxy settings to Engine.ini without disturbing anything else.

    `ca_cert`, when given, is handed to the SSL module as an extra trusted
    certificate so the game accepts the proxy's generated certificates.
    """
    lines = _strip_our_settings(_read_lines())
    lines += [
        "",
        MARKER,
        "[HTTP]",
        f"HttpProxyAddress=127.0.0.1:{proxy_port}",
        "",
        "[HTTP.Curl]",
        "bVerifyPeer=False",
        "",
        "[ConsoleVariables]",
        "n.VerifyPeer=0",
        "",
        "[SSL]",
        "bValidateRootCertificates=False",
    ]
    if ca_cert is not None:
        # Unreal wants forward slashes in ini paths.
        lines.append(f"DebuggingCertificatePath={ca_cert.as_posix()}")
    _write_lines(lines)
    return ENGINE_INI


def restore() -> bool:
    """Remove our settings again, leaving the rest of Engine.ini intact."""
    if not ENGINE_INI.exists():
        return False
    before = _read_lines()
    after = _strip_our_settings(before)
    if before == after:
        return False
    _write_lines(after)
    return True


def launch(
    *,
    proxy_port: int,
    dry_run: bool = False,
    extra_args: list[str] | None = None,
    via_steam: bool = False,
    ca_cert: Path | None = None,
) -> int:
    game = find_game()
    if game is None:
        print(
            "error: could not find the Phantom Abyss install.\n"
            "       edit KNOWN_GAME_PATHS in phantom_offline/client_config.py",
            file=sys.stderr,
        )
        return 1

    exe = find_exe(game)
    if exe is None:
        print(f"error: no launchable executable under {game}", file=sys.stderr)
        return 1

    # The proxy address is read from Engine.ini, so the command line only needs
    # to carry logging. That keeps the Steam launch path viable.
    game_args = ["-log", *(extra_args or [])]

    # A patched executable already points at the local server, so routing it
    # through the proxy as well only adds a hop and muddies the logs.
    patched = _is_patched(game)

    print(f"game     : {exe}")
    print(f"config   : {ENGINE_INI}")
    if patched:
        print("routing  : URL patch (direct to local server, no proxy)")
    else:
        print(f"proxy    : 127.0.0.1:{proxy_port}  (via Engine.ini)")
        if ca_cert is not None:
            status = "found" if ca_cert.exists() else "MISSING - start `serve` first"
            print(f"ca cert  : {ca_cert}  ({status})")
    if via_steam:
        print(f"command  : steam://run/{STEAM_APPID}//{' '.join(game_args)}")
    else:
        print(f"command  : {exe.name} {' '.join(game_args)}")

    if dry_run:
        print("\n(dry run -- no files written, game not launched)")
        return 0

    if patched:
        # Nothing to configure; make sure a previous run's proxy is gone.
        if restore():
            print("removed leftover proxy settings from Engine.ini")
    else:
        backup = backup_engine_ini()
        configure_proxy(proxy_port, ca_cert=ca_cert)
        if backup:
            print(f"backup   : {backup}")

    print("\nlaunching...\n")
    try:
        if via_steam:
            os.startfile(  # noqa: S606 - a steam:// URL, not a shell command
                f"steam://run/{STEAM_APPID}//{' '.join(game_args)}"
            )
        else:
            subprocess.Popen([str(exe), *game_args], cwd=str(exe.parent))
    except OSError as exc:
        print(f"error: failed to launch: {exc}", file=sys.stderr)
        return 1
    return 0
