import plistlib
from pathlib import Path

from pokemontcg_analyser import desktop


def test_install_app_writes_a_launcher_bundle(tmp_path: Path, monkeypatch) -> None:
    from pokemontcg_analyser import recorder

    monkeypatch.setattr(recorder, "build_capture_helper", lambda: None)  # no compiling in tests
    data_dir = tmp_path / "my data"
    data_dir.mkdir()

    bundle = desktop.install_app(data_dir=data_dir, port=8123, apps_dir=tmp_path / "Apps")

    assert bundle == tmp_path / "Apps" / "PTCG Live Analyser.app"
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "launcher"
    launcher = bundle / "Contents" / "MacOS" / "launcher"
    assert launcher.stat().st_mode & 0o111
    script = launcher.read_text()
    assert "-m pokemontcg_analyser.cli app --port 8123" in script
    assert f'--data-dir "{data_dir.resolve()}"' in script


def test_is_serving_false_when_nothing_listens() -> None:
    assert desktop.is_serving(1) is False
