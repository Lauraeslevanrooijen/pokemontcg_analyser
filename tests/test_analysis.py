from pathlib import Path

import cv2
import numpy as np

from pokemontcg_analyser import analysis


def _write_synthetic_video(path: Path, fps: int = 10) -> None:
    """A tiny video: 10 frames of solid gray, then 10 frames of solid white.
    The color jump midway should register as a scene change."""
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


def test_detect_scene_changes_flags_the_jump(tmp_path: Path) -> None:
    video_path = tmp_path / "synthetic.avi"
    _write_synthetic_video(video_path)

    changes = analysis.detect_scene_changes(video_path, sample_fps=10, threshold=12.0)

    assert len(changes) >= 1
    # the jump happens at frame 10 of a 10fps video, i.e. around 1.0s
    assert any(0.5 <= c.offset_seconds <= 1.5 for c in changes)


def test_detect_scene_changes_missing_file(tmp_path: Path) -> None:
    try:
        analysis.detect_scene_changes(tmp_path / "does-not-exist.avi")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")


def test_extract_frames_saves_expected_count(tmp_path: Path) -> None:
    video_path = tmp_path / "synthetic.avi"
    _write_synthetic_video(video_path)
    out_dir = tmp_path / "frames"

    saved = analysis.extract_frames(video_path, out_dir, sample_fps=5)

    assert len(saved) > 0
    assert all(p.exists() for p in saved)
