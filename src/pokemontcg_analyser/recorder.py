"""Screen recording for Pokémon TCG Live sessions.

macOS-only for now: shells out to ffmpeg's avfoundation input to capture a
screen (or window-containing display) to an MP4, alongside a JSON sidecar
recording wall-clock start time so later analysis can correlate video
timestamps with real time.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
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


def screen_devices() -> list[CaptureDevice]:
    """Just the screen-capture devices, without cameras or capture cards."""
    return [d for d in list_avfoundation_devices() if "Capture screen" in d.name]


def default_output_path(out_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return out_dir / f"{timestamp}.mp4"


def _write_sidecar(
    video_path: Path, device_index: int, framerate: int, started_at: datetime
) -> None:
    video_path.with_suffix(".json").write_text(
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


def add_mark(video_path: Path, offset_seconds: float) -> list[float]:
    """Remember a moment flagged while recording, in the video's sidecar, so
    it survives until the match is logged. Returns all marks so far."""
    sidecar_path = video_path.with_suffix(".json")
    sidecar = json.loads(sidecar_path.read_text())
    marks = [*sidecar.get("marks", []), round(offset_seconds, 2)]
    sidecar["marks"] = marks
    sidecar_path.write_text(json.dumps(sidecar, indent=2))
    return marks


def read_marks(video_path: Path) -> list[float]:
    try:
        return json.loads(video_path.with_suffix(".json").read_text()).get("marks", [])
    except (OSError, ValueError):
        return []


def build_ffmpeg_command(
    ffmpeg: str,
    device_index: int,
    video_path: Path,
    framerate: int = 30,
    capture_cursor: bool = True,
    max_width: int = 1920,
) -> list[str]:
    return [
        ffmpeg,
        # Needed by hwupload/scale_vt in the filter chain below.
        "-init_hw_device",
        "videotoolbox=vt",
        "-filter_hw_device",
        "vt",
        "-f",
        "avfoundation",
        # The device's default is uyvy422, which then needs a software
        # conversion before encoding; nv12 is what VideoToolbox takes
        # natively.
        "-pixel_format",
        "nv12",
        "-framerate",
        str(framerate),
        "-capture_cursor",
        "1" if capture_cursor else "0",
        "-i",
        f"{device_index}:none",
        # ffmpeg's avfoundation input holds a single pending frame: whenever
        # the rest of the pipeline falls behind, the next captured frame
        # silently replaces it. Output timestamps are therefore uneven (a
        # 370s libx264 recording once measured ~1.6fps average), so a
        # frame-count-based keyframe interval (-g) ends up wildly uneven in
        # real time. -r forces ffmpeg to pad with duplicate frames into a
        # true constant frame rate output, so -g below means what it says.
        "-r",
        str(framerate),
        "-vcodec",
        "h264_videotoolbox",
        # Software libx264 couldn't keep up in real time at Retina capture
        # resolutions, which left huge stretches of video with no keyframe
        # at all (seen: 8 keyframes across a 370s recording). Browsers need
        # a nearby keyframe to seek, so most of the video was unseekable and
        # jumped back to frame 0 instead. Hardware encoding keeps up in real
        # time.
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
        #
        # The scaling runs on the GPU (scale_vt): doing it in software took
        # ~80% of the whole pipeline's CPU time (measured 4.7s vs 0.9s of
        # CPU per 8s captured), and with the game running alongside that is
        # what makes the pipeline fall behind and drop frames mid-animation.
        "-vf",
        f"format=nv12,hwupload,scale_vt=w='min({max_width},iw)':h=-2",
        "-movflags",
        "+faststart",
        str(video_path),
    ]


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
    _write_sidecar(video_path, device_index, framerate, datetime.now(timezone.utc))

    cmd = build_ffmpeg_command(
        ffmpeg, device_index, video_path, framerate, capture_cursor, max_width
    )
    # ffmpeg reads a keypress on stdin to stop cleanly; run attached so the
    # user's Ctrl+C (SIGINT) reaches it directly and it flushes the file.
    subprocess.run(cmd)
    return video_path


@dataclass
class Recording:
    """A recording running in the background (started from the web app)."""

    video_path: Path
    log_path: Path
    started_at: datetime
    process: subprocess.Popen

    @property
    def is_running(self) -> bool:
        return self.process.poll() is None

    def mark(self) -> list[float]:
        """Flag the current moment; returns all marks so far."""
        elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        return add_mark(self.video_path, elapsed)

    def log_tail(self, lines: int = 8) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def stop(self, timeout: float = 15.0) -> Path:
        """Ask ffmpeg to finish and wait for it to finalize the file."""
        if self.is_running:
            try:
                # Same as pressing q in the terminal: ffmpeg stops reading
                # input and writes the MP4 index. Killing it instead would
                # leave a file no player can open.
                self.process.communicate(b"q", timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait()
        return self.video_path


def start_recording(
    device_index: int,
    out_dir: Path,
    framerate: int = 30,
    capture_cursor: bool = True,
    max_width: int = 1920,
) -> Recording:
    """Start recording in the background; call `.stop()` on the result.

    ffmpeg's output goes to a .log file next to the video. Raises
    RuntimeError if ffmpeg exits straight away (wrong device, or no Screen
    Recording permission for the app that launched this process).
    """
    ffmpeg = require_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = default_output_path(out_dir)
    log_path = video_path.with_suffix(".log")
    started_at = datetime.now(timezone.utc)
    _write_sidecar(video_path, device_index, framerate, started_at)

    cmd = build_ffmpeg_command(
        ffmpeg, device_index, video_path, framerate, capture_cursor, max_width
    )
    cmd[1:1] = ["-hide_banner", "-nostats"]
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT
        )
    recording = Recording(video_path, log_path, started_at, process)

    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        if not recording.is_running:
            raise RuntimeError(
                f"ffmpeg exited immediately:\n{recording.log_tail()}"
            )
        time.sleep(0.1)
    return recording


ORIGINALS_DIRNAME = "originals"


def video_duration(video_path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise FfmpegNotFoundError()
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def trim_start(video_path: Path, start_seconds: float) -> float:
    """Cut everything before `start_seconds` off the front of a recording.

    The video is copied, not re-encoded, so there is no quality loss — but a
    copy can only begin on a keyframe, so the cut lands on the last keyframe
    at or before `start_seconds` (up to 5s earlier). Returns how many seconds
    were actually removed. The untrimmed file is kept in an `originals`
    folder next to the recording until `purge_originals` clears it out.
    """
    ffmpeg = require_ffmpeg()
    before = video_duration(video_path)
    trimmed_path = video_path.with_name(f"{video_path.stem}.trimming.mp4")
    try:
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(start_seconds),
                "-i", str(video_path),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                str(trimmed_path),
            ],
            check=True,
            capture_output=True,
        )
        removed = round(max(0.0, before - video_duration(trimmed_path)), 3)
    except BaseException:
        trimmed_path.unlink(missing_ok=True)
        raise

    originals = video_path.parent / ORIGINALS_DIRNAME
    originals.mkdir(exist_ok=True)
    original_path = originals / video_path.name
    # On a second trim the file in originals/ is already the real original.
    if not original_path.exists():
        video_path.replace(original_path)
        # Its age counts from the trim, not from when it was recorded.
        os.utime(original_path)
    trimmed_path.replace(video_path)
    return removed


def purge_originals(recordings_dir: Path, max_age_days: float) -> list[Path]:
    """Delete untrimmed originals that were set aside more than
    `max_age_days` ago. Returns the files removed."""
    cutoff = time.time() - max_age_days * 86400
    removed = []
    for path in (recordings_dir / ORIGINALS_DIRNAME).glob("*.mp4"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path)
    return removed
