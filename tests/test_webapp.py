from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import time

from pokemontcg_analyser import cards, recorder, storage, transcribe, turns, webapp


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Static video serving is mounted once at import time (real recordings/
    # dir), so these tests only exercise routes/data, not actual file
    # serving — swapping the db is enough for that.
    monkeypatch.setattr(storage, "DEFAULT_DB_PATH", tmp_path / "matches.db")
    monkeypatch.setattr(webapp, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(webapp, "_recording", None)
    monkeypatch.setattr(webapp, "_recording_problem", None)
    monkeypatch.setattr(recorder, "game_crop", lambda: None)
    # No test should load the real speech model.
    monkeypatch.setattr(transcribe, "available", lambda: False)
    monkeypatch.setattr(
        recorder,
        "screens_and_microphones",
        lambda: (
            [recorder.CaptureDevice(index=3, name="Capture screen 0")],
            [
                recorder.CaptureDevice(index=0, name="iPhone Microphone"),
                recorder.CaptureDevice(index=1, name="MacBook Air Microphone"),
            ],
        ),
    )
    return TestClient(webapp.app)


def test_index_lists_matches(client: TestClient) -> None:
    storage.log_match(deck="Charizard ex", result="win")

    resp = client.get("/")

    assert resp.status_code == 200
    assert "Charizard ex" in resp.text


def test_create_match_via_form(client: TestClient) -> None:
    resp = client.post(
        "/matches",
        data={"deck": "Lost Box", "opponent_deck": "Gardevoir ex", "result": "loss"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    matches = storage.list_matches()
    assert len(matches) == 1
    assert matches[0].deck == "Lost Box"


def test_match_detail_404_for_missing_match(client: TestClient) -> None:
    resp = client.get("/matches/999")
    assert resp.status_code == 404


def test_match_detail_renders_for_real_match(client: TestClient) -> None:
    match_id = storage.log_match(deck="Charizard ex", result="win", video_file="recordings/x.mp4")

    resp = client.get(f"/matches/{match_id}")

    assert resp.status_code == 200
    assert "Charizard ex" in resp.text
    assert "/media/x.mp4" in resp.text


def test_add_note_round_trips(client: TestClient) -> None:
    match_id = storage.log_match(deck="Charizard ex", result="win")

    resp = client.post(
        f"/matches/{match_id}/notes",
        json={"text": "great opening hand", "offset_seconds": 12.5},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["detail"] == "great opening hand"
    assert body["offset_seconds"] == 12.5

    events = storage.list_events(match_id)
    assert len(events) == 1
    assert events[0].detail == "great opening hand"
    assert events[0].video_offset_seconds == 12.5


def test_add_note_404_for_missing_match(client: TestClient) -> None:
    resp = client.post("/matches/999/notes", json={"text": "hi"})
    assert resp.status_code == 404


class _FakeRecording:
    def __init__(self, video_path: Path) -> None:
        self.video_path = video_path
        self.started_at = datetime.now(timezone.utc)
        self.is_running = True

    def mark(self) -> list[float]:
        self.video_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar = self.video_path.with_suffix(".json")
        if not sidecar.exists():
            sidecar.write_text("{}")
        return recorder.add_mark(self.video_path, 12.5)

    def stop(self) -> Path:
        self.is_running = False
        self.video_path.parent.mkdir(parents=True, exist_ok=True)
        self.video_path.write_bytes(b"")
        return self.video_path


def test_start_and_stop_recording(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    started_with = []

    def fake_start(device_index: int, out_dir: Path, audio_index=None, crop=None, **options) -> _FakeRecording:
        started_with.append(device_index)
        return _FakeRecording(out_dir / "2026-01-01_120000.mp4")

    monkeypatch.setattr(recorder, "start_recording", fake_start)

    resp = client.post("/recording/start", follow_redirects=False)
    assert resp.status_code == 303
    assert started_with == [3]
    assert "Stop recording" in client.get("/").text
    assert client.post("/recording/start", follow_redirects=False).status_code == 409

    resp = client.post("/recording/stop", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/?video=2026-01-01_120000.mp4"

    page = client.get(resp.headers["location"]).text
    assert "Start recording" in page
    assert 'value="2026-01-01_120000.mp4" selected' in page
    assert "Recording saved" in page
    assert page.index("Recording saved") < page.index("<h1>Matches</h1>")


def test_stop_without_recording_is_409(client: TestClient) -> None:
    assert client.post("/recording/stop", follow_redirects=False).status_code == 409


def test_create_match_attaches_recording(client: TestClient) -> None:
    webapp.RECORDINGS_DIR.mkdir()
    (webapp.RECORDINGS_DIR / "game.mp4").write_bytes(b"")

    resp = client.post(
        "/matches",
        data={"deck": "Slob", "result": "win", "video_file": "game.mp4"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    match = storage.list_matches()[0]
    assert resp.headers["location"] == f"/matches/{match.id}"
    assert Path(match.video_file).name == "game.mp4"
    # once attached it is no longer offered for another match
    assert 'value="game.mp4"' not in client.get("/").text


def test_create_match_rejects_unknown_recording(client: TestClient) -> None:
    resp = client.post(
        "/matches",
        data={"deck": "Slob", "result": "win", "video_file": "../data/matches.db"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_marks_made_while_recording_land_on_the_match(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recorder,
        "start_recording",
        lambda device_index, out_dir, **options: _FakeRecording(out_dir / "marked.mp4"),
    )
    assert client.post("/recording/mark").status_code == 409

    client.post("/recording/start", follow_redirects=False)
    assert client.post("/recording/mark").json() == {"count": 1}
    assert client.post("/recording/mark").json() == {"count": 2}
    client.post("/recording/stop", follow_redirects=False)
    client.post(
        "/matches",
        data={"deck": "Slob", "result": "win", "video_file": "marked.mp4"},
        follow_redirects=False,
    )

    events = storage.list_events(storage.list_matches()[0].id)
    assert [(e.kind, e.video_offset_seconds) for e in events] == [("mark", 12.5), ("mark", 12.5)]


def test_label_only_note_and_empty_note(client: TestClient) -> None:
    match_id = storage.log_match(deck="Slob", result="win")

    resp = client.post(
        f"/matches/{match_id}/notes", json={"label": "misplay", "offset_seconds": 3.0}
    )
    assert resp.status_code == 200
    assert resp.json()["label"] == "misplay"
    assert resp.json()["detail"] is None

    assert client.post(f"/matches/{match_id}/notes", json={"text": "  "}).status_code == 400
    assert (
        client.post(f"/matches/{match_id}/notes", json={"text": "x", "label": "nope"}).status_code
        == 422
    )


def test_edit_and_delete_note(client: TestClient) -> None:
    match_id = storage.log_match(deck="Slob", result="win")
    note = client.post(
        f"/matches/{match_id}/notes", json={"text": "tpyo", "offset_seconds": 1.0}
    ).json()

    resp = client.patch(f"/events/{note['id']}", json={"text": "typo fixed", "label": "good"})
    assert resp.status_code == 200
    assert resp.json()["detail"] == "typo fixed"
    assert resp.json()["label"] == "good"
    assert resp.json()["offset_seconds"] == 1.0

    assert client.delete(f"/events/{note['id']}").status_code == 204
    assert storage.list_events(match_id) == []
    assert client.delete(f"/events/{note['id']}").status_code == 404


def test_editing_a_mark_turns_it_into_a_note(client: TestClient) -> None:
    match_id = storage.log_match(deck="Slob", result="win")
    mark_id = storage.add_event(match_id, "mark", offset_seconds=40.0)

    resp = client.patch(f"/events/{mark_id}", json={"text": "should have retreated"})

    assert resp.json()["kind"] == "note"
    assert resp.json()["offset_seconds"] == 40.0


def test_turn_markers(client: TestClient) -> None:
    match_id = storage.log_match(deck="Slob", result="win")

    turn = client.post(f"/matches/{match_id}/turns", json={"offset_seconds": 30.0}).json()

    assert turn["kind"] == "turn"
    assert client.patch(f"/events/{turn['id']}", json={"text": "x"}).status_code == 400
    assert client.delete(f"/events/{turn['id']}").status_code == 204


def test_set_and_clear_turn_order(client: TestClient) -> None:
    match_id = storage.log_match(deck="Slob", result="win")

    resp = client.put(f"/matches/{match_id}/turn-order", json={"turn_order": "second"})
    assert resp.status_code == 200
    assert storage.get_match(match_id).turn_order == "second"
    assert 'let turnOrder = "second";' in client.get(f"/matches/{match_id}").text

    client.put(f"/matches/{match_id}/turn-order", json={"turn_order": None})
    assert storage.get_match(match_id).turn_order is None

    assert (
        client.put(f"/matches/{match_id}/turn-order", json={"turn_order": "third"}).status_code
        == 422
    )
    assert client.put("/matches/999/turn-order", json={"turn_order": "first"}).status_code == 404


def test_game_start_and_trim_shift_the_timeline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    webapp.RECORDINGS_DIR.mkdir()
    (webapp.RECORDINGS_DIR / "game.mp4").write_bytes(b"")
    match_id = storage.log_match(deck="Slob", result="win", video_file="recordings/game.mp4")
    storage.add_note(match_id, "in the menu", offset_seconds=10.0)
    storage.add_note(match_id, "first attack", offset_seconds=100.0)

    assert client.post(f"/matches/{match_id}/trim").status_code == 400  # no game start yet

    resp = client.put(f"/matches/{match_id}/game-start", json={"offset_seconds": 62.0})
    assert resp.status_code == 200
    assert storage.get_match(match_id).game_start_seconds == 62.0

    trimmed = []

    def fake_trim(video_path: Path, start_seconds: float) -> float:
        trimmed.append((video_path.name, start_seconds))
        return 60.0  # the keyframe before the requested start

    monkeypatch.setattr(recorder, "trim_start", fake_trim)

    resp = client.post(f"/matches/{match_id}/trim")

    assert resp.json() == {"removed_seconds": 60.0}
    assert trimmed == [("game.mp4", 62.0)]
    assert storage.get_match(match_id).game_start_seconds == 2.0
    assert [e.video_offset_seconds for e in storage.list_events(match_id)] == [0.0, 40.0]


def test_voice_note_is_stored_served_and_deleted(client: TestClient) -> None:
    match_id = storage.log_match(deck="Slob", result="win")

    resp = client.post(
        f"/matches/{match_id}/voice",
        files={"audio": ("note", b"fake-opus-bytes", "audio/webm;codecs=opus")},
        data={"offset_seconds": "42.5"},
    )

    assert resp.status_code == 200
    note = resp.json()
    assert note["kind"] == "note"
    assert note["offset_seconds"] == 42.5
    name = note["audio_url"].removeprefix("/media/voice/")
    stored = webapp.RECORDINGS_DIR / "voice" / name
    assert stored.read_bytes() == b"fake-opus-bytes"

    # a spoken note needs no text, but can be given a label afterwards
    resp = client.patch(f"/events/{note['id']}", json={"text": "", "label": "misplay"})
    assert resp.status_code == 200
    assert resp.json()["audio_url"] == note["audio_url"]

    assert client.delete(f"/events/{note['id']}").status_code == 204
    assert not stored.exists()


def test_voice_note_rejects_non_audio(client: TestClient) -> None:
    match_id = storage.log_match(deck="Slob", result="win")

    resp = client.post(
        f"/matches/{match_id}/voice",
        files={"audio": ("note", b"<html>", "text/html")},
    )

    assert resp.status_code == 400
    assert storage.list_events(match_id) == []


def test_stats_and_moments_pages(client: TestClient) -> None:
    assert "No matches logged yet" in client.get("/stats").text

    match_id = storage.log_match(deck="Pult", opponent_deck="Iono", result="win")
    storage.add_note(match_id, "wrong attacker", offset_seconds=70.5, label="misplay")

    stats = client.get("/stats").text
    assert "Pult vs Iono" in stats
    assert "100%" in stats

    page = client.get("/moments").text
    assert "wrong attacker" in page
    assert f"/matches/{match_id}?t=70.50" in page
    assert "wrong attacker" not in client.get("/moments?label=good").text
    assert "wrong attacker" in client.get("/moments?label=all").text
    assert client.get("/moments?label=nope").status_code == 404


def test_lesson_is_saved_and_shown_on_the_start_page(client: TestClient) -> None:
    match_id = storage.log_match(deck="Pult", opponent_deck="Iono", result="loss")

    resp = client.put(f"/matches/{match_id}/lesson", json={"lesson": "  count prizes first  "})

    assert resp.json() == {"lesson": "count prizes first"}
    page = client.get("/").text
    assert "Remember from your last games" in page
    assert "count prizes first" in page

    client.put(f"/matches/{match_id}/lesson", json={"lesson": ""})
    assert storage.get_match(match_id).lesson is None
    assert "Remember from your last games" not in client.get("/").text


def test_filter_and_search_matches(client: TestClient) -> None:
    won = storage.log_match(deck="Pult", opponent_deck="Iono", result="win")
    storage.log_match(deck="Slob", opponent_deck="Zard", result="loss")
    storage.add_note(won, "forgot to RETREAT", offset_seconds=5.0)

    def rows(query: str) -> str:
        page = client.get("/" + query).text
        return page[page.index("<tbody>") : page.index("</tbody>")] if "<tbody>" in page else ""

    assert "Pult" in rows("") and "Slob" in rows("")
    assert "Pult" in rows("?result=win") and "Slob" not in rows("?result=win")
    assert "Slob" in rows("?deck=Slob") and "Iono" not in rows("?deck=Slob")
    assert "Zard" in rows("?q=zard") and "Iono" not in rows("?q=zard")
    # text on the timeline is searched too
    assert "Pult" in rows("?q=retreat") and "Slob" not in rows("?q=retreat")
    assert "No matches fit this filter" in client.get("/?q=nothing-like-this").text


def test_delete_recording_keeps_the_notes(client: TestClient) -> None:
    recordings = webapp.RECORDINGS_DIR
    (recordings / "originals").mkdir(parents=True)
    for path in ("game.mp4", "game.json", "originals/game.mp4", "other.mp4"):
        (recordings / path).write_bytes(b"x")
    match_id = storage.log_match(deck="Pult", result="win", video_file="recordings/game.mp4")
    storage.add_note(match_id, "keep me", offset_seconds=5.0)
    storage.set_game_start(match_id, 3.0)

    resp = client.post(f"/matches/{match_id}/recording/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert sorted(p.name for p in recordings.rglob("*") if p.is_file()) == ["other.mp4"]
    match = storage.get_match(match_id)
    assert match.video_file is None and match.game_start_seconds is None
    assert [e.detail for e in storage.list_events(match_id)] == ["keep me"]
    assert client.post(f"/matches/{match_id}/recording/delete").status_code == 400


def test_recording_with_voice_passes_the_microphone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = []

    def fake_start(device_index: int, out_dir: Path, audio_index=None, crop=None, **options) -> _FakeRecording:
        audio.append(audio_index)
        return _FakeRecording(out_dir / f"take{len(audio)}.mp4")

    monkeypatch.setattr(recorder, "start_recording", fake_start)

    mic = "MacBook Air Microphone"
    client.post("/recording/start", data={"voice": "true", "microphone": mic}, follow_redirects=False)
    client.post("/recording/stop", follow_redirects=False)
    # a microphone is chosen in the form but the box is not ticked
    client.post("/recording/start", data={"microphone": mic}, follow_redirects=False)
    client.post("/recording/stop", follow_redirects=False)

    assert audio == [1, None]
    resp = client.post("/recording/start", data={"voice": "true", "microphone": "Unplugged headset"})
    assert resp.status_code == 400


def test_decks_page_shows_versions_with_their_records(client: TestClient) -> None:
    storage.log_match(deck="Pult", result="loss")
    resp = client.post(
        "/decks",
        data={"deck": "Pult", "decklist": "4 Dragapult ex TWM 130\n3 Iono PAL 185"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    storage.log_match(deck="Pult", result="win")
    client.post(
        "/decks",
        data={"deck": "Pult", "decklist": "4 Dragapult ex TWM 130\n4 Iono PAL 185", "note": "max Iono"},
    )

    page = client.get("/decks").text

    assert "Before any saved list" in page
    assert "max Iono" in page
    assert "+1</span> Iono PAL 185" in page
    assert "8 cards" in page
    assert "<span>Dragapult ex</span>" in page
    assert "TWM 130" in page
    assert 'src="/cards/image/TWM/130"' in page
    assert client.post("/decks", data={"deck": " ", "decklist": "x"}).status_code == 400


def test_voice_note_is_written_out_in_the_background(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    heard = []

    def fake_transcribe(path: Path, vocabulary: list[str]) -> str:
        heard.append((path.name, vocabulary))
        return "ik had moeten terugtrekken"

    monkeypatch.setattr(transcribe, "available", lambda: True)
    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)
    storage.add_deck_version("Pult", "4 Dragapult ex TWM 130\n3 Iono PAL 185")
    match_id = storage.log_match(deck="Pult", result="win")

    note = client.post(
        f"/matches/{match_id}/voice",
        files={"audio": ("note", b"opus", "audio/webm")},
        data={"offset_seconds": "12"},
    ).json()
    assert note["transcribing"] is True

    deadline = time.time() + 5
    while client.get(f"/events/{note['id']}").json()["transcribing"]:
        assert time.time() < deadline, "transcription never finished"
        time.sleep(0.02)

    done = client.get(f"/events/{note['id']}").json()
    assert done["detail"] == "ik had moeten terugtrekken"
    assert done["audio_url"] == note["audio_url"]
    assert heard[0][1] == ["Pult", "Dragapult ex", "Iono"]


def test_transcribe_needs_a_voice_note_and_the_speech_model(client: TestClient) -> None:
    match_id = storage.log_match(deck="Pult", result="win")
    typed = client.post(f"/matches/{match_id}/notes", json={"text": "typed"}).json()
    spoken = client.post(
        f"/matches/{match_id}/voice", files={"audio": ("note", b"opus", "audio/webm")}
    ).json()

    assert spoken["transcribing"] is False  # not installed in this test
    assert client.post(f"/events/{typed['id']}/transcribe").status_code == 400
    assert client.post(f"/events/{spoken['id']}/transcribe").status_code == 503


def test_detect_turns_replaces_markers_and_fills_in_who_went_first(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    match_id = storage.log_match(deck="Pult", result="win", video_file="recordings/game.mp4")
    storage.add_event(match_id, "turn", offset_seconds=5.0)
    storage.add_note(match_id, "keep me", offset_seconds=50.0)
    monkeypatch.setattr(
        turns,
        "detect_turns",
        lambda path: [turns.Turn(63.0, "opponent"), turns.Turn(112.5, "you")],
    )

    resp = client.post(f"/matches/{match_id}/detect-turns")

    assert resp.status_code == 200
    body = resp.json()
    assert body["turn_order"] == "second"
    assert [(e["kind"], e["offset_seconds"], e["detail"]) for e in body["events"]] == [
        ("note", 50.0, "keep me"),
        ("turn", 63.0, "opponent"),
        ("turn", 112.5, "you"),
    ]
    assert storage.get_match(match_id).turn_order == "second"

    monkeypatch.setattr(turns, "detect_turns", lambda path: [])
    assert client.post(f"/matches/{match_id}/detect-turns").status_code == 422
    assert len(storage.list_events(match_id)) == 3  # nothing was wiped


def test_card_image_is_served_from_the_local_cache(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    picture = tmp_path / "sv06-130.webp"
    picture.write_bytes(b"webp-bytes")
    asked = []

    def fake_image_path(set_code: str, number: str):
        asked.append((set_code, number))
        return picture if number == "130" else None

    monkeypatch.setattr(cards, "image_path", fake_image_path)

    resp = client.get("/cards/image/TWM/130")
    assert resp.status_code == 200
    assert resp.content == b"webp-bytes"
    assert resp.headers["content-type"] == "image/webp"
    assert client.get("/cards/image/TWM/999").status_code == 404
    assert client.get("/cards/image/TW.M/130").status_code == 404
    assert asked == [("TWM", "130"), ("TWM", "999")]


def test_start_without_screen_recording_permission_explains_what_to_do(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args, **options):
        raise recorder.ScreenRecordingPermissionError()

    monkeypatch.setattr(recorder, "start_recording", denied)

    resp = client.post("/recording/start", follow_redirects=False)

    assert resp.status_code == 303  # not a bare error page
    page = client.get("/").text
    assert "has not allowed this app to record the screen" in page
    assert "Open Screen Recording settings" in page
    assert "Start recording" in page  # and it can be tried again

    monkeypatch.setattr(
        recorder, "start_recording", lambda *a, **o: _FakeRecording(webapp.RECORDINGS_DIR / "x.mp4")
    )
    client.post("/recording/start", follow_redirects=False)
    client.post("/recording/stop", follow_redirects=False)
    assert "has not allowed this app" not in client.get("/").text


def test_other_start_failures_show_the_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args, **options):
        raise RuntimeError("ffmpeg exited immediately:\nno such device")

    monkeypatch.setattr(recorder, "start_recording", broken)
    client.post("/recording/start", follow_redirects=False)

    assert "no such device" in client.get("/").text
