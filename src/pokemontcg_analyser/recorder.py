"""Screen recording for Pokémon TCG Live sessions.

macOS-only for now: shells out to ffmpeg's avfoundation input to capture a
screen (or window-containing display) to an MP4, alongside a JSON sidecar
recording wall-clock start time so later analysis can correlate video
timestamps with real time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class FfmpegNotFoundError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "ffmpeg was not found on PATH. Install it with `brew install ffmpeg`."
        )


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise FfmpegNotFoundError()
    return path


@dataclass(frozen=True)
class CaptureDevice:
    index: int
    name: str


def list_avfoundation_devices() -> list[CaptureDevice]:
    """List avfoundation video devices (screens, displays, capture cards).

    Parses ffmpeg's `-list_devices true` stderr output, which looks like:

        [AVFoundation indev @ 0x...] AVFoundation video devices:
        [AVFoundation indev @ 0x...] [0] FaceTime HD Camera
        [AVFoundation indev @ 0x...] [1] Capture screen 0
        [AVFoundation indev @ 0x...] AVFoundation audio devices:
        ...
    """
    ffmpeg = require_ffmpeg()
    result = subprocess.run(
        [ffmpeg, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
    )
    devices: list[CaptureDevice] = []
    in_video_section = False
    for line in result.stderr.splitlines():
        if "AVFoundation video devices" in line:
            in_video_section = True
            continue
        if "AVFoundation audio devices" in line:
            in_video_section = False
            continue
        if not in_video_section:
            continue
        marker = "] ["
        idx = line.find(marker)
        if idx == -1:
            continue
        rest = line[idx + len(marker) - 1 :]  # starts at "[<n>] name"
        close = rest.find("]")
        if close == -1:
            continue
        try:
            device_index = int(rest[1:close])
        except ValueError:
            continue
        name = rest[close + 1 :].strip()
        devices.append(CaptureDevice(index=device_index, name=name))
    return devices


def default_output_path(out_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return out_dir / f"{timestamp}.mp4"


def record(
    device_index: int,
    out_dir: Path,
    framerate: int = 30,
    capture_cursor: bool = True,
    max_width: int = 1920,
) -> Path:
    """Record the given avfoundation device to an MP4 until interrupted.

    Blocks until the ffmpeg process exits (e.g. via Ctrl+C, which ffmpeg
    handles gracefully and finalizes the file). Returns the output path.
    """
    ffmpeg = require_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = default_output_path(out_dir)
    sidecar_path = video_path.with_suffix(".json")

    started_at = datetime.now(timezone.utc)
    sidecar_path.write_text(
        json.dumps(
            {
                "video_file": video_path.name,
                "device_index": device_index,
                "framerate": framerate,
                "started_at_utc": started_at.isoformat(),
            },
            indent=2,
        )
    )

    cmd = [
        ffmpeg,
        "-f",
        "avfoundation",
        "-framerate",
        str(framerate),
        "-capture_cursor",
        "1" if capture_cursor else "0",
        "-i",
        f"{device_index}:none",
        # avfoundation only pushes a new frame when the screen actually
        # changes, not at a steady rate (a 370s recording once measured
        # ~1.6fps average) — so a frame-count-based keyframe interval (-g)
        # ends up wildly uneven in real time. -r forces ffmpeg to pad with
        # duplicate frames into a true constant frame rate output, so -g
        # below means what it says (a keyframe every ~2s of real time).
        "-r",
        str(framerate),
        "-vcodec",
        "h264_videotoolbox",
        # Software libx264 also couldn't keep up in real time at Retina
        # capture resolutions, which — combined with the sparse input above
        # — left huge stretches of video with no keyframe at all (seen: 8
        # keyframes across a 370s recording). Browsers need a nearby
        # keyframe to seek, so most of the video was unseekable and jumped
        # back to frame 0 instead. Hardware encoding keeps up in real time.
        #
        # A forced keyframe is a full intra-coded frame with no inter-frame
        # prediction, and this is UI/text content (expensive to intra-code)
        # sitting on top of a mostly-static screen — measured keyframes at
        # 300-600KB next to ~700-byte neighboring frames, a 700-800x jump
        # the browser has to decode in one shot. Forcing one every 2s (the
        # first fix) made that spike happen throughout the whole video, not
        # just where it was noticed. 5s keeps seeking plenty precise for
        # jumping to a labeled event while roughly halving how often it
        # happens, and the bitrate cap below shrinks each spike by ~2.5x
        # (measured 157KB->61KB) without visibly hurting card legibility.
        "-g",
        str(framerate * 5),
        "-b:v",
        "3M",
        "-maxrate",
        "5M",
        "-bufsize",
        "6M",
        # Native display resolution (e.g. 3420x2214 on a Retina screen) is
        # far more pixels than a browser can smoothly decode in real time,
        # and captures the whole desktop, not just the game — downscaling
        # keeps card text perfectly legible while cutting decode load
        # roughly in half and shrinking file size a lot.
        "-vf",
        f"scale='min({max_width},iw)':-2",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(video_path),
    ]
    # ffmpeg reads a keypress on stdin to stop cleanly; run attached so the
    # user's Ctrl+C (SIGINT) reaches it directly and it flushes the file.
    subprocess.run(cmd)
    return video_path
