"""Build the macOS app and a .dmg to hand out.

    pip install -e ".[package]"
    python packaging/build.py

Leaves dist/<App name>.app and dist/<App name>.dmg. The app carries its
own Python, ffmpeg and screen recorder, so the Mac it runs on needs nothing
installed. It is signed ad hoc, not by an Apple developer account: the
first time, macOS will refuse to open it until it is allowed under System
Settings > Privacy & Security.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

from pokemontcg_analyser import desktop, recorder

ROOT = Path(__file__).parent.parent
STAGING = ROOT / "build" / "staging"
DIST = ROOT / "dist"


def main() -> None:
    shutil.rmtree(STAGING, ignore_errors=True)
    STAGING.mkdir(parents=True)

    # A static ffmpeg (GPL build) from the imageio-ffmpeg package.
    shutil.copy2(imageio_ffmpeg.get_ffmpeg_exe(), STAGING / "ffmpeg")
    (STAGING / "ffmpeg").chmod(0o755)

    swiftc = shutil.which("swiftc")
    if swiftc is None:
        sys.exit("The Swift compiler is needed to build the screen recorder (xcode-select --install).")
    subprocess.run(
        [swiftc, *recorder.SWIFT_FLAGS, "-o", str(STAGING / "ptcg-capture"), str(recorder.CAPTURE_SOURCE)],
        check=True,
    )

    if desktop._write_icon(STAGING) is None:
        sys.exit("Could not make the app icon.")

    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"),
         str(ROOT / "packaging" / "app.spec")],
        check=True,
        cwd=ROOT,
        # Apple's own install_name_tool and lipo first: older ones from
        # MacPorts or conda, if they come earlier on the PATH, choke on the
        # libraries being packaged ("unknown load command").
        env={**os.environ, "PATH": "/usr/bin:" + os.environ.get("PATH", "")},
    )
    app = DIST / f"{desktop.APP_NAME}.app"
    shutil.rmtree(DIST / desktop.APP_NAME, ignore_errors=True)  # PyInstaller's unbundled copy
    # One ad-hoc signature over everything, so the permissions macOS asks
    # for (screen recording, microphone) stick to the app.
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)

    # The .dmg: the app next to a shortcut to Applications, to drag across.
    volume = ROOT / "build" / "dmg"
    shutil.rmtree(volume, ignore_errors=True)
    volume.mkdir(parents=True)
    shutil.copytree(app, volume / app.name, symlinks=True)
    (volume / "Applications").symlink_to("/Applications")
    dmg = DIST / f"{desktop.APP_NAME}.dmg"
    dmg.unlink(missing_ok=True)
    subprocess.run(
        ["hdiutil", "create", "-volname", desktop.APP_NAME, "-srcfolder", str(volume),
         "-ov", "-format", "UDZO", str(dmg)],
        check=True,
    )
    print(f"Built {dmg}")


if __name__ == "__main__":
    main()
