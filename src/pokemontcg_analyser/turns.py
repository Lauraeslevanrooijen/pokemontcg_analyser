"""Find turn changes in a recording from the ring around the playing field.

The ring is split in two: its upper half lights up (red) during the
opponent's turn, its lower half (blue) during yours, and the other half goes
dark. `turn_ring.png` marks which pixels of the game picture belong to each
half — measured from a real match by comparing a frame from each player's
turn — so reading a turn off a frame is just asking which half is bright.

Checked against a match with hand-placed turn markers: 6 of 7 found, five
within 3s of the marker; a second, unmarked match gave 14 cleanly
alternating turns. It has only seen one playmat and screen layout.
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
    """Where the game picture sits in the video frame, as x, y, w, h.

    Recordings made before capture was cropped to the game include black
    bars around the 16:9 picture; newer ones are the picture itself.
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
        if abs(w / h - 16 / 9) < 0.06:
            return x, y, w, h
    # Couldn't see the bars (a very dark recording): assume a centred 16:9.
    w = min(width, round(height * 16 / 9))
    h = round(w * 9 / 16)
    return (width - w) // 2, (height - h) // 2, w, h


def iter_picture_frames(video_path: Path, fps: float = SAMPLE_FPS) -> Iterator[np.ndarray]:
    """The game picture at WIDTH x HEIGHT, `fps` times per second of video."""
    x, y, w, h = picture_rect(video_path)
    process = subprocess.Popen(
        [
            require_ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-vf", f"fps={fps},crop={w}:{h}:{x}:{y},scale={WIDTH}:{HEIGHT}",
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
