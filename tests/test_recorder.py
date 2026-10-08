from unittest.mock import patch

from pokemontcg_analyser import recorder

FFMPEG_LIST_DEVICES_STDERR = """\
ffmpeg version 6.0
[AVFoundation indev @ 0x13e604a90] AVFoundation video devices:
[AVFoundation indev @ 0x13e604a90] [0] FaceTime HD Camera
[AVFoundation indev @ 0x13e604a90] [1] Capture screen 0
[AVFoundation indev @ 0x13e604a90] [2] Capture screen 1
[AVFoundation indev @ 0x13e604a90] AVFoundation audio devices:
[AVFoundation indev @ 0x13e604a90] [0] MacBook Pro Microphone
"""


class _FakeResult:
    def __init__(self, stderr: str) -> None:
        self.stderr = stderr


def test_list_avfoundation_devices_parses_video_section_only() -> None:
    with patch("pokemontcg_analyser.recorder.require_ffmpeg", return_value="ffmpeg"):
        with patch(
            "pokemontcg_analyser.recorder.subprocess.run",
            return_value=_FakeResult(FFMPEG_LIST_DEVICES_STDERR),
        ):
            devices = recorder.list_avfoundation_devices()

    assert devices == [
        recorder.CaptureDevice(index=0, name="FaceTime HD Camera"),
        recorder.CaptureDevice(index=1, name="Capture screen 0"),
        recorder.CaptureDevice(index=2, name="Capture screen 1"),
    ]


def test_list_avfoundation_devices_empty_output() -> None:
    with patch("pokemontcg_analyser.recorder.require_ffmpeg", return_value="ffmpeg"):
        with patch(
            "pokemontcg_analyser.recorder.subprocess.run",
            return_value=_FakeResult(""),
        ):
            devices = recorder.list_avfoundation_devices()

    assert devices == []


def test_require_ffmpeg_raises_when_missing() -> None:
    with patch("pokemontcg_analyser.recorder.shutil.which", return_value=None):
        try:
            recorder.require_ffmpeg()
        except recorder.FfmpegNotFoundError:
            pass
        else:
            raise AssertionError("expected FfmpegNotFoundError")


def test_build_ffmpeg_command_scales_on_gpu_to_max_width(tmp_path) -> None:
    out = tmp_path / "x.mp4"

    cmd = recorder.build_ffmpeg_command("ffmpeg", 3, out, framerate=30, max_width=1280)

    assert cmd[cmd.index("-i") + 1] == "3:none"
    assert cmd[cmd.index("-pixel_format") + 1] == "nv12"
    assert "scale_vt=w='min(1280,iw)':h=-2" in cmd[cmd.index("-vf") + 1]
    assert cmd[cmd.index("-g") + 1] == "150"
    assert cmd[-1] == str(out)


def test_screen_devices_excludes_cameras() -> None:
    with patch("pokemontcg_analyser.recorder.require_ffmpeg", return_value="ffmpeg"):
        with patch(
            "pokemontcg_analyser.recorder.subprocess.run",
            return_value=_FakeResult(FFMPEG_LIST_DEVICES_STDERR),
        ):
            devices = recorder.screen_devices()

    assert [d.index for d in devices] == [1, 2]


def test_purge_originals_only_removes_old_files(tmp_path) -> None:
    import os
    import time

    originals = tmp_path / recorder.ORIGINALS_DIRNAME
    originals.mkdir()
    old, fresh = originals / "old.mp4", originals / "fresh.mp4"
    old.write_bytes(b"")
    fresh.write_bytes(b"")
    (tmp_path / "current.mp4").write_bytes(b"")
    three_weeks_ago = time.time() - 21 * 86400
    os.utime(old, (three_weeks_ago, three_weeks_ago))

    removed = recorder.purge_originals(tmp_path, max_age_days=14)

    assert removed == [old]
    assert fresh.exists()
    assert (tmp_path / "current.mp4").exists()


def test_purge_originals_without_originals_folder(tmp_path) -> None:
    assert recorder.purge_originals(tmp_path, max_age_days=14) == []
