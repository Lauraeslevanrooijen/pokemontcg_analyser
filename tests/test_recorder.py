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
