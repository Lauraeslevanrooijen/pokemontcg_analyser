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
        "-vcodec",
        "libx264",
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
