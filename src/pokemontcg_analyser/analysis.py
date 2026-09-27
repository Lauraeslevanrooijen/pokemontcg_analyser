"""First-pass video analysis: sample frames and flag scene changes.

Full card recognition needs a calibrated pipeline (knowing where the hand,
board, and prize zones sit on screen) built from real recorded footage. As a
generic starting point that needs no UI-specific calibration, this module
samples frames at a fixed interval and flags timestamps where the frame
changed significantly — candidate boundaries for turns, card plays, or
board-state changes that a human (or a later, smarter pass) can review.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class SceneChange:
    offset_seconds: float
    score: float


def _frame_diff_score(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute pixel difference between two grayscale frames, 0-255."""
    return float(np.mean(cv2.absdiff(a, b)))


def detect_scene_changes(
    video_path: Path,
    sample_fps: float = 2.0,
    threshold: float = 12.0,
) -> list[SceneChange]:
    """Sample the video at `sample_fps` and return frames whose difference
    from the previous sampled frame exceeds `threshold`.

    Threshold is a mean-abs-diff over grayscale 0-255 pixel values; higher
    means less sensitive. 12.0 is a reasonable starting point for catching
    real board-state changes while ignoring minor animation/shimmer.
    """
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_interval = max(1, round(video_fps / sample_fps))

    changes: list[SceneChange] = []
    prev_gray: np.ndarray | None = None
    frame_index = 0

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_index % frame_interval == 0:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.resize(gray, (320, 180))
                if prev_gray is not None:
                    score = _frame_diff_score(gray, prev_gray)
                    if score >= threshold:
                        offset = frame_index / video_fps
                        changes.append(SceneChange(offset_seconds=offset, score=score))
                prev_gray = gray
            frame_index += 1
    finally:
        cap.release()

    return changes


def extract_frames(
    video_path: Path,
    out_dir: Path,
    sample_fps: float = 1.0,
) -> list[Path]:
    """Extract frames at `sample_fps` to `out_dir` as numbered JPEGs."""
    if not video_path.exists():
        raise FileNotFoundError(video_path)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_interval = max(1, round(video_fps / sample_fps))

    saved: list[Path] = []
    frame_index = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame_index % frame_interval == 0:
                offset = frame_index / video_fps
                out_path = out_dir / f"frame_{offset:08.2f}.jpg"
                cv2.imwrite(str(out_path), frame)
                saved.append(out_path)
            frame_index += 1
    finally:
        cap.release()

    return saved
