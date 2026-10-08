from pathlib import Path

import cv2
import numpy as np

from pokemontcg_analyser import analysis


def _write_synthetic_video(path: Path, fps: int = 10) -> None:
    """A tiny video: 10 frames of solid gray, then 10 frames of solid white."""
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (64, 64))
    assert writer.isOpened(), "test setup: VideoWriter failed to open"
    try:
        gray = np.full((64, 64, 3), 60, dtype=np.uint8)
        white = np.full((64, 64, 3), 250, dtype=np.uint8)
        for _ in range(10):
            writer.write(gray)
        for _ in range(10):
            writer.write(white)
    finally:
        writer.release()


def test_extract_frames_saves_expected_count(tmp_path: Path) -> None:
    video_path = tmp_path / "synthetic.avi"
    _write_synthetic_video(video_path)
    out_dir = tmp_path / "frames"

    saved = analysis.extract_frames(video_path, out_dir, sample_fps=5)

    assert len(saved) > 0
    assert all(p.exists() for p in saved)
