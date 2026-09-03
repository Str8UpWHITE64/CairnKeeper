"""Freeze the client into a single executable.

    python build.py            build it
    python build.py --check    report what would happen, and why

The client is deliberately pure standard library, so there is very little to
bundle. That is not only about download size: a frozen interpreter that patches
another program's executable is close to the textbook shape of a trojan, and
every compiled library left out is one less thing for a scanner to weigh.

Expect a warning anyway. An unsigned executable that nobody has seen before
gets flagged, and that is a reasonable thing for a scanner to do. The answer is
not to argue with it -- publish the hash, keep the source open, and say so
plainly before anyone downloads it.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "CairnKeeper"
ENTRY = ROOT / "launch.py"

# Left out on purpose. Nothing in the play path imports these, and every one
# excluded is bulk removed and one less compiled object in the bundle.
EXCLUDE = [
    "cryptography",      # TLS proxy only; the supervisor always patches instead
    "pytest",
    "numpy",
    "pandas",
    "matplotlib",
    "PIL",
    "setuptools",
    "pip",
]


def write_entry() -> Path:
    """A tiny entry point, so the frozen build starts at the window."""
    ENTRY.write_text(
        '"""Entry point for the packaged build: open the window."""\n'
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "from phantom_offline.__main__ import main\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    # No arguments means the window; anything else behaves as the CLI,\n"
        "    # so the packaged build is still usable from a terminal.\n"
        "    if len(sys.argv) == 1:\n"
        "        sys.argv.append('window')\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    return ENTRY


def check() -> int:
    print("Build check")
    print()
    ok = True

    print(f"  python           : {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        print("     needs 3.11 or newer")
        ok = False

    try:
        import PyInstaller  # noqa: F401

        print(f"  pyinstaller      : {PyInstaller.__version__}")
    except ImportError:
        print("  pyinstaller      : NOT INSTALLED  (pip install pyinstaller)")
        ok = False

    try:
        import tkinter  # noqa: F401

        print(f"  window toolkit   : tkinter {tkinter.TkVersion}")
    except ImportError:
        print("  window toolkit   : MISSING -- the build would be CLI only")

    # The claim this build rests on: nothing on the play path is third-party.
    import builtins

    real_import = builtins.__import__
    outside = set()

    def watch(name, *a, **k):
        root = name.split(".")[0]
        if root in {"cryptography"}:
            outside.add(root)
        return real_import(name, *a, **k)

    builtins.__import__ = watch
    try:
        for module in ("server", "supervisor", "backend", "library",
                       "fidelity", "bundles", "gui", "patch"):
            __import__(f"phantom_offline.{module}")
    finally:
        builtins.__import__ = real_import

    if outside:
        print(f"  stdlib only      : NO -- pulls in {', '.join(sorted(outside))}")
        ok = False
    else:
        print("  stdlib only      : yes, nothing third-party on the play path")

    print()
    print("Ready to build." if ok else "Not ready -- see above.")
    return 0 if ok else 1


def build() -> int:
    if check() != 0:
        return 1

    entry = write_entry()
    out = ROOT / "dist"
    locked = False
    if out.exists():
        try:
            shutil.rmtree(out)
        except PermissionError:
            # Windows will not let you replace a running executable, and the
            # obvious reason it is running is that somebody is testing the last
            # build. Refusing to build would mean the tester has to stop testing
            # to get the fix they are waiting for, so build beside it instead.
            locked = True
            out = ROOT / "dist-new"
            if out.exists():
                shutil.rmtree(out, ignore_errors=True)
            print()
            print(f"  {NAME}.exe is running, so this build goes to dist-new/")

    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", NAME,
        "--noconfirm",
        "--clean",
        # A console window would flash up behind the window on every launch.
        "--windowed",
        "--distpath", str(out),
        *sum((["--exclude-module", m] for m in EXCLUDE), []),
        str(entry),
    ]
    print("\n" + " ".join(args) + "\n")
    result = subprocess.run(args, cwd=str(ROOT))
    if result.returncode != 0:
        return result.returncode

    built = out / f"{NAME}.exe"
    if not built.exists():
        print("the build reported success but produced nothing")
        return 1

    digest = hashlib.sha256(built.read_bytes()).hexdigest()
    size = built.stat().st_size / 1e6
    print()
    print(f"  {built}")
    print(f"  {size:.1f} MB")
    print(f"  sha256 {digest}")
    (out / "SHA256.txt").write_text(f"{digest}  {NAME}.exe\n", encoding="utf-8")
    print()
    if locked:
        print("  The copy you are running is the OLD one. Close it, then either")
        print(f"  run this build from dist-new/ or move it over dist/.")
    else:
        print("Publish that hash alongside the download. Windows will warn about")
        print("an unsigned executable it has not seen before -- say so up front")
        print("rather than letting people discover it themselves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(check() if "--check" in sys.argv else build())
