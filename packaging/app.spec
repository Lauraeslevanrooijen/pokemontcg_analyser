# PyInstaller recipe for the macOS app. Run through packaging/build.py, which
# first puts ffmpeg, the screen recorder and the icon in build/staging/.

from pathlib import Path

from pokemontcg_analyser import desktop

root = Path(SPECPATH).parent
package = root / "src" / "pokemontcg_analyser"
staging = root / "build" / "staging"

analysis = Analysis(
    [str(root / "packaging" / "app_main.py")],
    pathex=[str(root / "src")],
    datas=[
        (str(package / "templates"), "pokemontcg_analyser/templates"),
        (str(package / "turn_ring.png"), "pokemontcg_analyser"),
    ],
    # As binaries, so they are signed along with the app.
    binaries=[
        (str(staging / "ffmpeg"), "pokemontcg_analyser/bin"),
        (str(staging / "ptcg-capture"), "pokemontcg_analyser/bin"),
    ],
    hiddenimports=["pokemontcg_analyser.webapp", "uvicorn.logging", "uvicorn.loops.auto",
                   "uvicorn.protocols.http.auto", "uvicorn.lifespan.on"],
    # The speech model and its libraries would add several hundred MB; the
    # packaged app leaves spoken notes untranscribed instead.
    excludes=["faster_whisper", "ctranslate2", "onnxruntime", "tokenizers", "huggingface_hub",
              "av", "imageio_ffmpeg", "pytest", "tkinter"],
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    exclude_binaries=True,
    name=desktop.APP_NAME,
    console=False,
    target_arch="arm64",
)
collected = COLLECT(exe, analysis.binaries, analysis.datas, name=desktop.APP_NAME)
app = BUNDLE(
    collected,
    name=f"{desktop.APP_NAME}.app",
    icon=str(staging / "icon.icns"),
    bundle_identifier=desktop.BUNDLE_ID,
    info_plist={
        "CFBundleShortVersionString": "0.1.0",
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "To record spoken notes and, if you choose, your voice while you play.",
        # The app talks to its own local server over plain http.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
