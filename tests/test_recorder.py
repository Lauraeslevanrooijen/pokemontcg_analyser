from pathlib import Path
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


def test_audio_devices_parses_the_audio_section() -> None:
    with patch("pokemontcg_analyser.recorder.require_ffmpeg", return_value="ffmpeg"):
        with patch(
            "pokemontcg_analyser.recorder.subprocess.run",
            return_value=_FakeResult(FFMPEG_LIST_DEVICES_STDERR),
        ):
            devices = recorder.audio_devices()

    assert devices == [recorder.CaptureDevice(index=0, name="MacBook Pro Microphone")]


def test_build_ffmpeg_command_with_microphone(tmp_path) -> None:
    silent = recorder.build_ffmpeg_command("ffmpeg", 3, tmp_path / "x.mp4")
    voiced = recorder.build_ffmpeg_command("ffmpeg", 3, tmp_path / "x.mp4", audio_index=1)

    assert silent[silent.index("-i") + 1] == "3:none"
    assert "-c:a" not in silent
    assert voiced[voiced.index("-i") + 1] == "3:1"
    assert voiced[voiced.index("-c:a") + 1] == "aac"


def test_fit_game_area_letterboxes_a_fullscreen_window() -> None:
    # fullscreen below the notch on a 1710x1107 display
    x, y, w, h = recorder.fit_game_area((1710, 1107), (0, 34, 1710, 1073))

    assert (x, w) == (0, 1)
    assert round(y * 2214) == 179  # measured in a real capture: 174
    assert round(h * 2214) == 1924


def test_fit_game_area_pillarboxes_a_wide_window() -> None:
    x, y, w, h = recorder.fit_game_area((2000, 1000), (0, 100, 2000, 450))

    assert round(w * 2000) == 800 and round(h * 1000) == 450
    assert round(x * 2000) == 600 and round(y * 1000) == 100


def test_fit_game_area_gives_up_when_off_screen_or_pointless() -> None:
    assert recorder.fit_game_area((1710, 1107), (1710, 0, 1920, 1080)) is None  # other display
    assert recorder.fit_game_area((1920, 1080), (0, 0, 1920, 1080)) is None  # nothing to cut


def test_build_ffmpeg_command_crops_before_scaling(tmp_path) -> None:
    cmd = recorder.build_ffmpeg_command(
        "ffmpeg", 3, tmp_path / "x.mp4", crop=(0.0, 0.0808, 1.0, 0.869)
    )

    assert cmd[cmd.index("-vf") + 1].startswith(
        "crop=iw*1.00000:ih*0.86900:iw*0.00000:ih*0.08080,format=nv12,hwupload"
    )


def test_capture_helper_name_follows_its_source(tmp_path, monkeypatch) -> None:
    source = tmp_path / "main.swift"
    source.write_text("// one")
    monkeypatch.setattr(recorder, "CAPTURE_SOURCE", source)
    first = recorder.capture_helper_path()
    source.write_text("// two")

    assert recorder.capture_helper_path() != first
    assert first.name.startswith("ptcg-capture-")


def test_build_capture_helper_without_a_compiler(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(recorder, "CAPTURE_CACHE_DIR", tmp_path)
    with patch("pokemontcg_analyser.recorder.shutil.which", return_value=None):
        assert recorder.build_capture_helper() is None


def test_start_recording_uses_the_helper_for_the_main_display(tmp_path, monkeypatch) -> None:
    helper = tmp_path / "ptcg-capture-test"
    # stands in for the real recorder: announce, then wait to be stopped
    helper.write_text("#!/bin/sh\necho \"recording 1920x1080 at 30 fps\"\necho \"$@\"\nread line\nexit 0\n")
    helper.chmod(0o755)
    monkeypatch.setattr(recorder, "capture_helper_path", lambda: helper)

    recording = recorder.start_recording(
        3, tmp_path / "out", crop=(0.0, 0.1, 1.0, 0.8), audio_name="Desk Mic", main_display=True
    )
    recording.stop()

    log = recording.log_path.read_text()
    assert "--crop 0.00000,0.10000,1.00000,0.80000" in log
    assert "--microphone Desk Mic" in log
    assert recording.process.returncode == 0


def _script(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_helper_without_permission_raises_instead_of_falling_back(tmp_path, monkeypatch) -> None:
    helper = _script(tmp_path, "helper", 'echo "error: screen-recording-permission: not allowed" >&2\nexit 1\n')
    monkeypatch.setattr(recorder, "capture_helper_path", lambda: helper)
    monkeypatch.setattr(recorder, "require_ffmpeg", lambda: (_ for _ in ()).throw(AssertionError("ffmpeg used")))
    out = tmp_path / "out"

    try:
        recorder.start_recording(3, out, main_display=True)
    except recorder.ScreenRecordingPermissionError:
        pass
    else:
        raise AssertionError("expected ScreenRecordingPermissionError")
    assert list(out.iterdir()) == []  # nothing left behind


def test_ffmpeg_that_never_writes_is_called_off(tmp_path, monkeypatch) -> None:
    # like ffmpeg waiting on a screen it may not record: alive, silent, deaf
    stuck = _script(tmp_path, "ffmpeg", "trap '' TERM\nwhile true; do sleep 1; done\n")
    monkeypatch.setattr(recorder, "require_ffmpeg", lambda: str(stuck))
    monkeypatch.setattr(recorder.time, "monotonic", _fast_clock())
    out = tmp_path / "out"

    try:
        recorder.start_recording(3, out)
    except recorder.ScreenRecordingPermissionError:
        pass
    else:
        raise AssertionError("expected ScreenRecordingPermissionError")
    assert list(out.iterdir()) == []


def _fast_clock():
    """A clock that jumps ahead two seconds per look, to skip the waiting."""
    now = [0.0]

    def monotonic() -> float:
        now[0] += 2.0
        return now[0]

    return monotonic


def test_stop_twice_and_with_a_deaf_process(tmp_path) -> None:
    import subprocess
    from datetime import datetime, timezone

    stuck = _script(tmp_path, "stuck", "trap '' TERM\nwhile true; do sleep 1; done\n")
    process = subprocess.Popen([str(stuck)], stdin=subprocess.PIPE)
    recording = recorder.Recording(tmp_path / "x.mp4", tmp_path / "x.log", datetime.now(timezone.utc), process)

    recording.stop(timeout=0.2)  # ignores q and SIGTERM; gets killed
    recording.stop(timeout=0.2)  # nothing left to do, and no error

    assert not recording.is_running


def test_video_duration_is_read_from_ffmpeg_output(monkeypatch) -> None:
    report = "Input #0, mov,mp4\n  Duration: 00:07:23.20, start: 0.000000, bitrate: 1846 kb/s\n"
    monkeypatch.setattr(recorder, "require_ffmpeg", lambda: "ffmpeg")
    with patch("pokemontcg_analyser.recorder.subprocess.run", return_value=_FakeResult(report)):
        assert recorder.video_duration(Path("x.mp4")) == 443.2

    with patch("pokemontcg_analyser.recorder.subprocess.run", return_value=_FakeResult("no such file")):
        try:
            recorder.video_duration(Path("x.mp4"))
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


def test_bundled_tools_are_preferred(tmp_path, monkeypatch) -> None:
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("")
    monkeypatch.setattr(recorder, "BUNDLED_FFMPEG", ffmpeg)
    helper = tmp_path / "ptcg-capture"
    helper.write_text("")
    monkeypatch.setattr(recorder, "BUNDLED_CAPTURE_HELPER", helper)

    assert recorder.require_ffmpeg() == str(ffmpeg)
    assert recorder.capture_helper_path() == helper
