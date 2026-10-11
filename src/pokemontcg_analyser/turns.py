"""Find turn changes in a recording from the ring around the playing field.

The ring is split in two: its upper half lights up (red) during the
opponent's turn, its lower half (blue) during yours, and the other half goes
dark. `turn_ring.png` marks which pixels of the game picture belong to each
half — measured from a real match by comparing a frame from each player's
turn — so reading a turn off a frame is just asking which half is bright.

Checked against a match with hand-placed turn markers: 6 of 7 found, five
within 3s of the marker, and against battle logs since: the number of
turns and whose they are match.

Where the ring sits in a recording is not fixed: the game draws its board
larger in a window than fullscreen, and a recording may be cut to the game
or show the whole screen. So the ring is looked for first (`find_layout`)
and every frame is brought to the picture the mask was measured on.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

import cv2
import numpy as np

from .recorder import require_ffmpeg

Owner = Literal["you", "opponent"]

RING_MASK_PATH = Path(__file__).parent / "turn_ring.png"
# In the mask image: which half of the ring a pixel belongs to.
MASK_OPPONENT, MASK_YOU = 255, 128

# Everything is measured on the game picture scaled to this size.
WIDTH, HEIGHT = 480, 270
SAMPLE_FPS = 4
# How much brighter (0-255) one ring half must be than the other. Measured
# ~215 lit against ~85 dark; in a recording where the picture sat a few
# pixels off the mask it was still 150 against 80. With both halves dark
# (a dialog over the board) or both alike, the frame is left unread.
MIN_DIFFERENCE = 40
# A new state has to hold this long before it counts, so a flash or an
# animation crossing the ring doesn't register as a turn.
HOLD_SECONDS = 1.0


@dataclass(frozen=True)
class Turn:
    offset_seconds: float
    owner: Owner


def picture_rect(video_path: Path, samples: int = 8) -> tuple[int, int, int, int]:
    """The part of the video frame that isn't black bars, as x, y, w, h.

    Recordings of a fullscreen game have black bars around the picture;
    most others are the picture itself.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    try:
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        brightest = np.zeros((height, width), dtype=np.uint8)
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_count * (i + 1) / (samples + 1)))
            ok, frame = cap.read()
            if ok:
                brightest = np.maximum(brightest, frame.max(axis=2))
    finally:
        cap.release()

    # A row or column belongs to the picture if a fair share of it is ever
    # lit; a stray bright dot in a black bar (the camera indicator) is not.
    rows = np.where((brightest > 24).mean(axis=1) > 0.05)[0]
    cols = np.where((brightest > 24).mean(axis=0) > 0.05)[0]
    if rows.size and cols.size:
        x, y = int(cols[0]), int(rows[0])
        w, h = int(cols[-1]) - x + 1, int(rows[-1]) - y + 1
        # (a very dark recording could leave next to nothing: keep it all then)
        if w * h >= 0.5 * width * height:
            return x, y, w - w % 2, h - h % 2
    return 0, 0, width, height


def iter_raw_frames(video_path: Path, fps: float = SAMPLE_FPS) -> Iterator[np.ndarray]:
    """The recording at WIDTH x HEIGHT, `fps` times per second: black bars
    removed, scaled to the frame's height and centred (cut or padded at the
    sides when it isn't 16:9). Where the playing field sits in that still
    depends on how the game was shown; see `layout`."""
    x, y, w, h = picture_rect(video_path)
    process = subprocess.Popen(
        [
            require_ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-vf",
            f"fps={fps},crop={w}:{h}:{x}:{y},scale=-2:{HEIGHT},"
            f"pad='max(iw,{WIDTH})':{HEIGHT}:(ow-iw)/2:0,crop={WIDTH}:{HEIGHT}",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "-",
        ],
        stdout=subprocess.PIPE,
    )
    frame_bytes = WIDTH * HEIGHT * 3
    try:
        while True:
            data = process.stdout.read(frame_bytes)
            if len(data) < frame_bytes:
                break
            yield np.frombuffer(data, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)
    finally:
        process.stdout.close()
        process.kill()
        process.wait()


@dataclass(frozen=True)
class Layout:
    """How the playing field sits in a recording compared with the picture
    the ring mask was measured on: `zoom` times as large, and moved `dx`,
    `dy` pixels (of the WIDTH x HEIGHT frame) from the centre."""

    zoom: float = 1.0
    dx: float = 0.0
    dy: float = 0.0

    def matrix(self) -> np.ndarray:
        """Maps a raw frame onto the picture the mask was measured on."""
        cx, cy = WIDTH / 2, HEIGHT / 2
        scale = 1 / self.zoom
        return np.float32(
            [
                [scale, 0, cx - (cx + self.dx) * scale],
                [0, scale, cy - (cy + self.dy) * scale],
            ]
        )


# Recordings are not all cut the same way: the game shows its board larger
# in a window than fullscreen, and a window can sit anywhere on the screen.
_ZOOMS = np.arange(0.75, 1.36, 0.01)
_MAX_SHIFT = 60  # pixels either way, at WIDTH x HEIGHT


def find_layout(frames: list[np.ndarray]) -> Layout:
    """Find the ring in a spread of frames from one recording.

    Over a game each half of the ring keeps switching between lit and dark,
    so its pixels vary far more than anything around them. The picture of
    how much each pixel varies shows the ring; the mask is slid and scaled
    over it until it fits best.
    """
    if len(frames) < 8:
        return Layout()
    brightness = np.stack([frame.max(axis=2) for frame in frames]).astype(np.float32)
    varies = brightness.std(axis=0)
    mask = (cv2.imread(str(RING_MASK_PATH), cv2.IMREAD_GRAYSCALE) > 0).astype(np.float32)
    ys, xs = np.where(mask > 0)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    ring = mask[y0:y1, x0:x1]
    # room around the frame, so a ring partly outside it can still be found
    margin = 160
    field = cv2.copyMakeBorder(varies, margin, margin, margin, margin, cv2.BORDER_CONSTANT, value=0)
    candidates: list[tuple[float, Layout]] = []
    for zoom in _ZOOMS:
        size = (max(8, round(ring.shape[1] * zoom)), max(8, round(ring.shape[0] * zoom)))
        template = cv2.resize(ring, size, interpolation=cv2.INTER_AREA)
        # where the mask would lie at this zoom without any shift
        left = WIDTH / 2 + (x0 - WIDTH / 2) * zoom + margin
        top = HEIGHT / 2 + (y0 - HEIGHT / 2) * zoom + margin
        xa, ya = int(round(left - _MAX_SHIFT)), int(round(top - _MAX_SHIFT))
        window = field[ya : ya + size[1] + 2 * _MAX_SHIFT, xa : xa + size[0] + 2 * _MAX_SHIFT]
        if window.shape[0] < size[1] or window.shape[1] < size[0]:
            continue
        scores = cv2.matchTemplate(window, template, cv2.TM_CCORR_NORMED)
        _, score, _, (bx, by) = cv2.minMaxLoc(scores)
        candidates.append((score, Layout(float(zoom), float(bx - _MAX_SHIFT), float(by - _MAX_SHIFT))))

    # The shape of what varies can be matched by other things than the ring
    # (in a short game there is little to go on). The best-fitting few are
    # therefore judged on what the ring is for: with the right layout one
    # half is clearly brighter than the other in nearly every frame.
    full_mask = cv2.imread(str(RING_MASK_PATH), cv2.IMREAD_GRAYSCALE)
    shortlist = [found for _, found in sorted(candidates, key=lambda c: -c[0])[:12]] + [Layout()]
    return max(shortlist, key=lambda found: _contrast(brightness, full_mask, found))


def _contrast(brightness: np.ndarray, mask: np.ndarray, found: Layout) -> float:
    """How far apart the two ring halves are in brightness, on average over
    the frames, when the mask is laid over them as `found` says."""
    halves = []
    for value in (MASK_OPPONENT, MASK_YOU):
        ys, xs = np.where(mask == value)
        rx = np.round(WIDTH / 2 + found.dx + (xs - WIDTH / 2) * found.zoom).astype(int)
        ry = np.round(HEIGHT / 2 + found.dy + (ys - HEIGHT / 2) * found.zoom).astype(int)
        inside = (rx >= 0) & (rx < WIDTH) & (ry >= 0) & (ry < HEIGHT)
        if inside.sum() < 0.6 * len(xs):
            return 0.0  # most of this half would be off the picture
        halves.append(brightness[:, ry[inside], rx[inside]].mean(axis=1))
    return float(np.abs(halves[0] - halves[1]).mean())


def sample_frames(video_path: Path, count: int = 90) -> list[np.ndarray]:
    """Frames spread evenly over a recording, as `iter_raw_frames` gives them."""
    duration = None
    try:
        from .recorder import video_duration

        duration = video_duration(video_path)
    except (ValueError, OSError):
        pass
    fps = count / duration if duration else 0.2
    return list(iter_raw_frames(video_path, fps=fps))


_layouts: dict[tuple[str, float], Layout] = {}


def layout(video_path: Path) -> Layout:
    """The layout of a recording, worked out once per file."""
    key = (str(video_path), video_path.stat().st_mtime)
    if key not in _layouts:
        _layouts[key] = find_layout(sample_frames(video_path))
    return _layouts[key]


def iter_picture_frames(video_path: Path, fps: float = SAMPLE_FPS) -> Iterator[np.ndarray]:
    """The game picture as the ring mask (and the card sizes used to find
    plays) expect it, whatever way the recording was cut."""
    found = layout(video_path)
    if found == Layout():
        yield from iter_raw_frames(video_path, fps)
        return
    matrix = found.matrix()
    for frame in iter_raw_frames(video_path, fps):
        yield cv2.warpAffine(frame, matrix, (WIDTH, HEIGHT), flags=cv2.INTER_LINEAR)


def ring_state(frame: np.ndarray, mask: np.ndarray) -> Owner | None:
    """Whose turn a frame shows, or None when the ring can't be read
    (covered by a dialog, between turns, or not in a game at all)."""
    brightness = frame.max(axis=2)
    opponent = brightness[mask == MASK_OPPONENT].mean()
    you = brightness[mask == MASK_YOU].mean()
    if opponent - you > MIN_DIFFERENCE:
        return "opponent"
    if you - opponent > MIN_DIFFERENCE:
        return "you"
    return None


def turns_from_states(states: list[Owner | None], fps: float = SAMPLE_FPS) -> list[Turn]:
    """Collapse per-frame readings into turn starts. Unreadable frames keep
    the turn that was running, and a change must persist to count."""
    hold = max(1, round(HOLD_SECONDS * fps))
    turns: list[Turn] = []
    current: Owner | None = None
    candidate: Owner | None = None
    candidate_start = 0
    run = 0
    for index, state in enumerate(states):
        if state is None or state == current:
            candidate, run = None, 0
            continue
        if state == candidate:
            run += 1
        else:
            candidate, candidate_start, run = state, index, 1
        if run >= hold:
            current = state
            turns.append(Turn(offset_seconds=candidate_start / fps, owner=state))
            candidate, run = None, 0
    return turns


def detect_turns(video_path: Path) -> list[Turn]:
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    mask = cv2.imread(str(RING_MASK_PATH), cv2.IMREAD_GRAYSCALE)
    states = [ring_state(frame, mask) for frame in iter_picture_frames(video_path)]
    return turns_from_states(states)
