"""Frame extraction from recorded videos.

Full card recognition needs a calibrated pipeline (knowing where the hand,
board, and prize zones sit on screen) built from real recorded footage. As a
starting point that needs no UI-specific calibration, this module samples
frames at a fixed interval to disk for inspection.
"""

from __future__ import annotations

from pathlib import Path

import cv2


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
