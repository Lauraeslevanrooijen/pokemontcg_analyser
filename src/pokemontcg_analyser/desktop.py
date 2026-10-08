"""Run the web app as a desktop app: its own window, no terminal.

The window is a Chromium-family browser in "app mode" (no tabs or address
bar) with a profile of its own, so it behaves like a separate application
and quitting it (Cmd+Q) is what shuts the local server down again.
`install_app` wraps that in a double-clickable macOS .app bundle.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "Pokemon TCG Analyser"
BUNDLE_ID = "local.pokemontcg-analyser"
PAGE_MARKER = "Pokémon TCG Live analyser"

BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]


def find_browser() -> str | None:
    return next((b for b in BROWSERS if Path(b).exists()), None)


def is_serving(port: int) -> bool:
    """Whether this app (not something else) already answers on `port`."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as resp:
            return PAGE_MARKER in resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError):
        return False


def open_window(url: str, profile_dir: Path) -> subprocess.Popen | None:
    """Open the app window. Returns the browser process, or None when no
    app-mode browser is installed and the default browser was used instead."""
    browser = find_browser()
    if browser is None:
        webbrowser.open(url)
        return None
    profile_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            browser,
            f"--app={url}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1500,950",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run(port: int = 8000, data_dir: Path | None = None) -> None:
    """Serve the app and show it in its own window until that is quit."""
    import uvicorn

    # The database and recordings live relative to the working directory,
    # and the web app resolves them when it is imported — so move first.
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(data_dir)
    url = f"http://127.0.0.1:{port}"
    profile_dir = Path("data/app-profile").resolve()

    if is_serving(port):
        # Already running (launched twice): just bring up another window.
        open_window(url, profile_dir)
        return

    server = uvicorn.Server(
        uvicorn.Config("pokemontcg_analyser.webapp:app", host="127.0.0.1", port=port)
    )

    def window_lifecycle() -> None:
        while not server.started:
            if server.should_exit:
                return
            time.sleep(0.1)
        browser = open_window(url, profile_dir)
        if browser is None:
            return  # default browser: nothing to wait on, serve until killed
        browser.wait()
        # Stopping through uvicorn runs the app's shutdown, which finalizes
        # a recording that is still in progress.
        server.should_exit = True

    threading.Thread(target=window_lifecycle, daemon=True).start()
    server.run()


def _write_icon(resources: Path) -> str | None:
    """Draw a simple icon (a record dot on a dark tile) and convert it with
    macOS's own tools. Purely cosmetic, so any failure just means no icon."""
    try:
        import cv2
        import numpy as np

        size = 1024
        image = np.zeros((size, size, 4), dtype=np.uint8)
        cv2.rectangle(image, (112, 112), (912, 912), (33, 26, 23, 255), -1, cv2.LINE_AA)
        cv2.rectangle(image, (112, 112), (912, 912), (33, 26, 23, 255), 120, cv2.LINE_AA)
        cv2.circle(image, (512, 512), 300, (50, 182, 242, 255), 56, cv2.LINE_AA)
        cv2.circle(image, (512, 512), 150, (75, 83, 229, 255), -1, cv2.LINE_AA)
        iconset = resources / "icon.iconset"
        iconset.mkdir()
        master = resources / "icon.png"
        cv2.imwrite(str(master), image)
        for points in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = points * scale
                suffix = "@2x" if scale == 2 else ""
                subprocess.run(
                    ["sips", "-z", str(pixels), str(pixels), str(master),
                     "--out", str(iconset / f"icon_{points}x{points}{suffix}.png")],
                    check=True, capture_output=True,
                )
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "icon.icns")],
            check=True, capture_output=True,
        )
        shutil.rmtree(iconset)
        master.unlink()
        return "icon"
    except Exception:
        return None


def install_app(data_dir: Path, port: int = 8000, apps_dir: Path | None = None) -> Path:
    """Create a double-clickable .app that runs `run()` against `data_dir`.

    The bundle is only a launcher: it starts this Python environment, so it
    keeps working as the code changes but breaks if the project or its
    virtualenv is moved (re-run the install then).
    """
    from . import recorder

    recorder.build_capture_helper()
    apps_dir = apps_dir if apps_dir is not None else Path.home() / "Applications"
    bundle = apps_dir / f"{APP_NAME}.app"
    if bundle.exists():
        shutil.rmtree(bundle)
    macos = bundle / "Contents" / "MacOS"
    resources = bundle / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()

    data_dir = data_dir.resolve()
    # Apps started from Finder get a bare PATH without Homebrew, where
    # ffmpeg usually lives.
    ffmpeg = shutil.which("ffmpeg")
    path_dirs = [str(Path(ffmpeg).parent)] if ffmpeg else []
    path_dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    launcher = macos / "launcher"
    launcher.write_text(
        "#!/bin/bash\n"
        f'export PATH="{":".join(dict.fromkeys(path_dirs))}"\n'
        f'mkdir -p "{data_dir}/data"\n'
        f'exec "{sys.executable}" -m pokemontcg_analyser.cli app '
        f'--port {port} --data-dir "{data_dir}" >> "{data_dir}/data/app.log" 2>&1\n'
    )
    launcher.chmod(0o755)

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "launcher",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        # The window belongs to the browser; keep the launcher itself out
        # of the Dock.
        "LSUIElement": True,
    }
    icon = _write_icon(resources)
    if icon:
        info["CFBundleIconFile"] = icon
    with (bundle / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump(info, f)

    # An ad-hoc signature gives macOS a stable identity to attach the
    # Screen Recording permission to.
    subprocess.run(["codesign", "--force", "--sign", "-", str(bundle)], capture_output=True)
    return bundle
