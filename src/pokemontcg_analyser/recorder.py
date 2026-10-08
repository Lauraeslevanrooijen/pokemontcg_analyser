"""Screen recording for Pokémon TCG Live sessions.

macOS-only for now: shells out to ffmpeg's avfoundation input to capture a
screen (or window-containing display) to an MP4, alongside a JSON sidecar
recording wall-clock start time so later analysis can correlate video
timestamps with real time.
"""

from __future__ import annotations

import hashlib
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


def _list_devices() -> dict[str, list[CaptureDevice]]:
    """Parse ffmpeg's `-list_devices true` stderr output into its "video"
    and "audio" sections. It looks like:

        [AVFoundation indev @ 0x...] AVFoundation video devices:
        [AVFoundation indev @ 0x...] [0] FaceTime HD Camera
        [AVFoundation indev @ 0x...] [1] Capture screen 0
        [AVFoundation indev @ 0x...] AVFoundation audio devices:
        [AVFoundation indev @ 0x...] [0] MacBook Pro Microphone
    """
    ffmpeg = require_ffmpeg()
    result = subprocess.run(
        [ffmpeg, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True,
        text=True,
    )
    devices: dict[str, list[CaptureDevice]] = {"video": [], "audio": []}
    current_section = None
    for line in result.stderr.splitlines():
        if "AVFoundation video devices" in line:
            current_section = "video"
            continue
        if "AVFoundation audio devices" in line:
            current_section = "audio"
            continue
        if current_section is None:
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
        devices[current_section].append(CaptureDevice(index=device_index, name=name))
    return devices


def list_avfoundation_devices() -> list[CaptureDevice]:
    """List avfoundation video devices (screens, displays, capture cards)."""
    return _list_devices()["video"]


def audio_devices() -> list[CaptureDevice]:
    """List avfoundation audio inputs (microphones)."""
    return _list_devices()["audio"]


def screens_and_microphones() -> tuple[list[CaptureDevice], list[CaptureDevice]]:
    """Both lists from a single ffmpeg call."""
    devices = _list_devices()
    return [d for d in devices["video"] if "Capture screen" in d.name], devices["audio"]


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


GAME_APP_NAME = "Pokemon TCG Live"

# Asks the window server (no extra permission needed) for the main display's
# size and every normal window of one app, as JSON.
_WINDOW_SCRIPT = """
ObjC.import('CoreGraphics');
function run(argv) {
  const display = $.CGDisplayBounds($.CGMainDisplayID());
  const windows = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo(0, 0)))
    .filter((w) => w.kCGWindowLayer === 0 && w.kCGWindowOwnerName === argv[0])
    .map((w) => w.kCGWindowBounds);
  return JSON.stringify({ display: [display.size.width, display.size.height], windows });
}
"""

Crop = tuple[float, float, float, float]  # x, y, width, height as fractions of the screen


def fit_game_area(display: tuple[float, float], window: tuple[float, float, float, float]) -> Crop | None:
    """Where the game picture sits on the main display, given its window.

    The game always draws 16:9, centred in its window with black bars for
    the rest (measured fullscreen on a 1710x1107 display: picture at
    y=174..2099 of 2214 pixels, this predicts 179..2103). Returns None when
    cropping would gain nothing or the window isn't on the main display.
    """
    display_w, display_h = display
    x, y, w, h = window
    picture_w = min(w, h * 16 / 9)
    picture_h = picture_w * 9 / 16
    left = x + (w - picture_w) / 2
    top = y + (h - picture_h) / 2
    if left < 0 or top < 0 or left + picture_w > display_w or top + picture_h > display_h:
        return None
    if picture_w * picture_h > 0.98 * display_w * display_h:
        return None
    return (left / display_w, top / display_h, picture_w / display_w, picture_h / display_h)


def game_crop(app_name: str = GAME_APP_NAME) -> Crop | None:
    """The part of the main display showing the game, or None if the game
    isn't open (or anything about asking goes wrong — then record it all)."""
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _WINDOW_SCRIPT, app_name],
            capture_output=True, text=True, timeout=5, check=True,
        )
        info = json.loads(result.stdout)
        windows = [(b["X"], b["Y"], b["Width"], b["Height"]) for b in info["windows"]]
        if not windows:
            return None
        # The game window is the big one; the app also owns thin bar windows.
        window = max(windows, key=lambda b: b[2] * b[3])
        return fit_game_area(tuple(info["display"]), window)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


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
    audio_index: int | None = None,
    crop: Crop | None = None,
) -> list[str]:
    """`audio_index` adds a microphone to the recording, for talking
    through your plays while you make them. `crop` keeps only that part of
    the screen (see `game_crop`)."""
    audio = [] if audio_index is None else ["-c:a", "aac", "-b:a", "128k"]
    # Cropping raw frames is free (it only moves pointers), so it goes
    # before the upload to the GPU.
    crop_filter = ""
    if crop is not None:
        x, y, w, h = (f"{value:.5f}" for value in crop)
        crop_filter = f"crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
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
        f"{device_index}:{'none' if audio_index is None else audio_index}",
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
        f"{crop_filter}format=nv12,hwupload,scale_vt=w='min({max_width},iw)':h=-2",
        *audio,
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


CAPTURE_SOURCE = Path(__file__).parent / "capture" / "main.swift"
CAPTURE_CACHE_DIR = Path.home() / "Library" / "Caches" / "pokemontcg-analyser"


def capture_helper_path() -> Path:
    """Where the compiled ScreenCaptureKit recorder lives. The name carries
    a hash of its source, so editing the source means a fresh build."""
    digest = hashlib.sha1(CAPTURE_SOURCE.read_bytes()).hexdigest()[:12]
    return CAPTURE_CACHE_DIR / f"ptcg-capture-{digest}"


def build_capture_helper() -> Path | None:
    """Compile the recorder if that hasn't been done yet (about 20s, once).
    Returns None when there is no Swift compiler or the build fails; callers
    then record with ffmpeg instead."""
    helper = capture_helper_path()
    if helper.exists():
        return helper
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        return None
    CAPTURE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    building = helper.with_suffix(f".building{os.getpid()}")
    result = subprocess.run(
        [swiftc, "-O", "-swift-version", "5", "-o", str(building), str(CAPTURE_SOURCE)],
        capture_output=True,
    )
    if result.returncode != 0 or not building.exists():
        building.unlink(missing_ok=True)
        return None
    building.replace(helper)
    for old in CAPTURE_CACHE_DIR.glob("ptcg-capture-*"):
        if old != helper:
            old.unlink(missing_ok=True)
    return helper


def _launch(cmd: list[str], log_path: Path, mode: str) -> subprocess.Popen:
    with log_path.open(mode) as log:
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)


def start_recording(
    device_index: int,
    out_dir: Path,
    framerate: int = 30,
    capture_cursor: bool = True,
    max_width: int = 1920,
    audio_index: int | None = None,
    crop: Crop | None = None,
    audio_name: str | None = None,
    main_display: bool = False,
) -> Recording:
    """Start recording in the background; call `.stop()` on the result.

    The main display is recorded with the ScreenCaptureKit helper when it
    has been built: ffmpeg only gets ~12 frames a second from macOS while a
    fullscreen game is in front. Anything else, or a helper that won't
    start, falls back to ffmpeg. The recorder's output goes to a .log file
    next to the video. Raises RuntimeError if recording can't start (wrong
    device, or no Screen Recording permission for the app that launched
    this process).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    video_path = default_output_path(out_dir)
    log_path = video_path.with_suffix(".log")
    started_at = datetime.now(timezone.utc)
    _write_sidecar(video_path, device_index, framerate, started_at)

    helper = capture_helper_path() if main_display else None
    if helper is not None and helper.exists():
        cmd = [str(helper), "--output", str(video_path), "--max-width", str(max_width), "--fps", str(framerate)]
        if crop is not None:
            cmd += ["--crop", ",".join(f"{value:.5f}" for value in crop)]
        if audio_name is not None:
            cmd += ["--microphone", audio_name]
        if not capture_cursor:
            cmd.append("--no-cursor")
        recording = Recording(video_path, log_path, started_at, _launch(cmd, log_path, "wb"))
        # It reports "recording WxH" once frames are flowing.
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and recording.is_running:
            if "recording " in recording.log_tail():
                # Marks are measured from here, so count from the first frame.
                recording.started_at = datetime.now(timezone.utc)
                return recording
            time.sleep(0.1)
        if recording.is_running:
            recording.process.kill()
            recording.process.wait()
        video_path.unlink(missing_ok=True)
        with log_path.open("ab") as log:
            log.write(b"\n-- the ScreenCaptureKit recorder did not start; using ffmpeg --\n")

    ffmpeg = require_ffmpeg()
    cmd = build_ffmpeg_command(
        ffmpeg, device_index, video_path, framerate, capture_cursor, max_width, audio_index, crop
    )
    cmd[1:1] = ["-hide_banner", "-nostats"]
    recording = Recording(video_path, log_path, started_at, _launch(cmd, log_path, "ab"))

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
