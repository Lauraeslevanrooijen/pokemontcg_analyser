from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import json
import time

from pokemontcg_analyser import cards, logtimes, recorder, storage, transcribe, turns, webapp


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Static video serving is mounted once at import time (real recordings/
    # dir), so these tests only exercise routes/data, not actual file
    # serving — swapping the db is enough for that.
    monkeypatch.setattr(storage, "DEFAULT_DB_PATH", tmp_path / "matches.db")
    monkeypatch.setattr(webapp, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(webapp, "_recording", None)
    monkeypatch.setattr(webapp, "_recording_problem", None)
    # Backups go to a real folder outside the project; never from a test.
    monkeypatch.setattr(webapp, "BACKUP_DIR", tmp_path / "backups")
    # No test should read a real video; those that want turns say so.
    monkeypatch.setattr(turns, "detect_turns", lambda path: [])
    monkeypatch.setattr(logtimes, "find_play_times", lambda *args: {})
    # The card database is on the internet; tests that need a card say so.
    monkeypatch.setattr(cards, "lookup", lambda set_code, number: None)
    monkeypatch.setattr(recorder, "video_duration", lambda path: 600.0)
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
    yield TestClient(webapp.app)
    # Let background work finish before the next test swaps the database.
    deadline = time.time() + 5
    while (webapp._detecting or webapp._transcribing) and time.time() < deadline:
        time.sleep(0.01)


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
    assert resp.headers["location"] == f"/matches/{match.id}?wrapup=1"
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


def test_battle_log_is_stored_and_corrects_order_and_result(client: TestClient) -> None:
    text = (Path(__file__).parent / "data" / "battle_log.txt").read_text()
    match_id = storage.log_match(deck="Pult", result="loss")  # logged wrongly

    resp = client.put(f"/matches/{match_id}/battle-log", json={"text": text})

    assert resp.status_code == 200
    body = resp.json()
    assert (body["turn_order"], body["result"], body["result_changed"]) == ("second", "win", True)
    assert [t["owner"] for t in body["battle_log"]["turns"]] == ["opponent", "you", "opponent", "you"]
    assert body["battle_log"]["opponent_pokemon"] == ["Poltchageist", "Dhelmise"]
    match = storage.get_match(match_id)
    assert (match.turn_order, match.result) == ("second", "win")
    assert "Played Buddy-Buddy Poffin." in client.get(f"/matches/{match_id}").text
    # what happened in a game is searchable from the match list
    page = client.get("/?q=poltchageist").text
    assert "Pult" in page[page.index("<tbody>") :]

    assert client.put(f"/matches/{match_id}/battle-log", json={"text": "not a log"}).status_code == 400
    assert storage.get_match(match_id).battle_log == text.strip()

    client.put(f"/matches/{match_id}/battle-log", json={"text": ""})
    assert storage.get_match(match_id).battle_log is None
    assert storage.get_match(match_id).result == "win"  # removing the log undoes nothing


def test_logging_a_match_with_a_battle_log_takes_the_result_from_it(client: TestClient) -> None:
    text = (Path(__file__).parent / "data" / "battle_log.txt").read_text()

    resp = client.post(
        "/matches",
        data={"deck": "Pult", "result": "loss", "battle_log": text},
        follow_redirects=False,
    )

    match = storage.list_matches()[0]
    assert resp.headers["location"] == f"/matches/{match.id}?wrapup=1"
    assert (match.result, match.turn_order) == ("win", "second")
    assert match.battle_log.startswith("Setup")

    resp = client.post("/matches", data={"deck": "Pult", "result": "win", "battle_log": "nonsense"})
    assert resp.status_code == 400
    assert len(storage.list_matches()) == 1


def test_battle_log_names_the_opponent_and_reuses_a_name_given_earlier(client: TestClient) -> None:
    text = (Path(__file__).parent / "data" / "battle_log.txt").read_text()
    data = {"deck": "Pult", "result": "win", "battle_log": text}

    client.post("/matches", data=data, follow_redirects=False)
    first = storage.list_matches()[0]
    assert first.opponent_deck == "Dhelmise / Poltchageist"  # from what the opponent played

    # I rename it; the next game against the same Pokémon gets my name
    storage.set_opponent_deck(first.id, "Banette control")
    client.post("/matches", data=data, follow_redirects=False)
    newest = max(storage.list_matches(), key=lambda m: m.id)
    assert newest.opponent_deck == "Banette control"

    # a name typed in the form is left alone
    client.post("/matches", data={**data, "opponent_deck": "My own name"}, follow_redirects=False)
    assert max(storage.list_matches(), key=lambda m: m.id).opponent_deck == "My own name"


def test_stats_show_opening_hands_from_battle_logs(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    kinds = {"Buddy-Buddy Poffin": ("Trainer", None, "Item"), "Budew": ("Pokemon", "Basic", None)}

    def fake_lookup(set_code: str, number: str):
        name = {"1": "Buddy-Buddy Poffin", "2": "Budew"}.get(number)
        if name is None:
            return None
        category, stage, trainer = kinds[name]
        return cards.Card("x", name, None, category, stage, trainer)

    monkeypatch.setattr(cards, "lookup", fake_lookup)
    text = (Path(__file__).parent / "data" / "battle_log.txt").read_text()
    storage.log_match(deck="Slob", result="loss")  # a match without a log
    assert "Nothing here until a match has a battle log" in client.get("/stats").text

    client.post("/matches", data={"deck": "Pult", "result": "win", "battle_log": text})
    page = client.get("/stats").text

    assert "Based on the 1 game with a battle log" in page
    assert "Buddy-Buddy Poffin" in page and "Munkidori" in page
    # no saved list, so no Supporter/Basic counts yet
    assert "Save the decklist on the Decks page" in page


def _wait_for_detection(client: TestClient, match_id: int) -> dict:
    deadline = time.time() + 5
    while True:
        timeline = client.get(f"/matches/{match_id}/timeline").json()
        if not timeline["detecting"]:
            return timeline
        assert time.time() < deadline, "turn detection never finished"
        time.sleep(0.02)


def test_turns_are_detected_in_the_background_when_a_recording_is_logged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    webapp.RECORDINGS_DIR.mkdir()
    (webapp.RECORDINGS_DIR / "game.mp4").write_bytes(b"")
    monkeypatch.setattr(
        turns, "detect_turns", lambda path: [turns.Turn(63.0, "opponent"), turns.Turn(112.5, "you")]
    )

    client.post(
        "/matches",
        data={"deck": "Pult", "result": "win", "video_file": "game.mp4"},
        follow_redirects=False,
    )
    match_id = storage.list_matches()[0].id
    timeline = _wait_for_detection(client, match_id)

    assert [(e["kind"], e["detail"]) for e in timeline["events"]] == [
        ("turn", "opponent"),
        ("turn", "you"),
    ]
    assert timeline["turn_order"] == "second"


def test_background_detection_leaves_existing_turn_markers_alone(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    match_id = storage.log_match(deck="Pult", result="win", video_file="recordings/game.mp4")
    storage.add_event(match_id, "turn", offset_seconds=5.0)
    monkeypatch.setattr(turns, "detect_turns", lambda path: [turns.Turn(63.0, "opponent")])

    webapp.detect_turns_in_background(match_id)
    timeline = _wait_for_detection(client, match_id)

    assert [e["offset_seconds"] for e in timeline["events"]] == [5.0]


def test_comment_on_a_battle_log_line(client: TestClient) -> None:
    text = (Path(__file__).parent / "data" / "battle_log.txt").read_text()
    client.post("/matches", data={"deck": "Pult", "result": "win", "battle_log": text})
    match_id = storage.list_matches()[0].id

    note = client.post(
        f"/matches/{match_id}/notes",
        json={"text": "should have benched Meowth first", "label": "misplay", "log_turn": 2, "log_action": 4},
    ).json()

    assert (note["log_turn"], note["log_action"], note["offset_seconds"]) == (2, 4, None)
    # editing keeps it on its line
    edited = client.patch(f"/events/{note['id']}", json={"text": "bench order", "label": "misplay"}).json()
    assert (edited["log_turn"], edited["log_action"], edited["detail"]) == (2, 4, "bench order")
    # it counts as a misplay in turn 2, though it has no place in a video
    page = client.get("/moments").text
    assert "bench order" in page and "Turn 2" in page
    assert client.delete(f"/events/{note['id']}").status_code == 204


def test_edit_and_delete_a_match(client: TestClient) -> None:
    recordings = webapp.RECORDINGS_DIR
    (recordings / "voice").mkdir(parents=True)
    for name in ("game.mp4", "game.json", "voice/9-abc.webm", "other.mp4"):
        (recordings / name).write_bytes(b"x")
    storage.add_deck_version("Pult", "4 Dragapult ex TWM 130")
    match_id = storage.log_match(deck="Plut", opponent_deck="iono", result="loss", video_file="recordings/game.mp4")
    storage.add_event(match_id, "note", offset_seconds=5.0, audio_file="9-abc.webm")

    resp = client.put(f"/matches/{match_id}", json={"deck": "Pult", "opponent_deck": "Gardevoir", "result": "win"})
    assert resp.status_code == 200
    match = storage.get_match(match_id)
    assert (match.deck, match.opponent_deck, match.result) == ("Pult", "Gardevoir", "win")
    assert match.deck_version_id is not None  # now counts towards Pult's saved list
    assert client.put(f"/matches/{match_id}", json={"deck": " ", "result": "win"}).status_code == 400

    resp = client.post(f"/matches/{match_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert storage.get_match(match_id) is None
    assert storage.list_all_events() == []
    assert sorted(p.name for p in recordings.rglob("*") if p.is_file()) == ["other.mp4"]


def test_matchup_note_shows_on_every_match_against_that_deck(client: TestClient) -> None:
    first = storage.log_match(deck="Pult", opponent_deck="Tauros", result="loss")
    second = storage.log_match(deck="Pult", opponent_deck="Tauros", result="win")

    client.put("/matchups/note", json={"opponent_deck": "Tauros", "note": "keep Budew off the Active Spot"})

    for match_id in (first, second):
        assert "keep Budew off the Active Spot" in client.get(f"/matches/{match_id}").text
    assert "keep Budew off the Active Spot" in client.get("/").text
    client.put("/matchups/note", json={"opponent_deck": "Tauros", "note": ""})
    assert storage.matchup_notes() == {}


def test_old_recordings_are_deleted_only_once_a_period_is_chosen(client: TestClient) -> None:
    recordings = webapp.RECORDINGS_DIR
    recordings.mkdir()
    for name in ("old.mp4", "new.mp4"):
        (recordings / name).write_bytes(b"x")
    old = storage.log_match(deck="Pult", result="win", video_file="recordings/old.mp4")
    new = storage.log_match(deck="Pult", result="win", video_file="recordings/new.mp4")
    storage.add_note(old, "still here", offset_seconds=5.0)
    with storage.connect() as conn:
        conn.execute("UPDATE matches SET played_at_utc = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old,))

    client.get("/")
    assert (recordings / "old.mp4").exists()  # nothing is deleted by default

    client.post("/settings/recording-retention", data={"weeks": "8"}, follow_redirects=False)
    client.get("/")

    assert not (recordings / "old.mp4").exists()
    assert (recordings / "new.mp4").exists()
    assert storage.get_match(old).video_file is None
    assert storage.get_match(new).video_file is not None
    assert [e.detail for e in storage.list_events(old)] == ["still here"]
    assert client.post("/settings/recording-retention", data={"weeks": "soon"}).status_code == 400


def test_daily_backup_of_database_and_spoken_notes(client: TestClient) -> None:
    voice = webapp.RECORDINGS_DIR / "voice"
    voice.mkdir(parents=True)
    (voice / "1-abc.webm").write_bytes(b"spoken")
    storage.log_match(deck="Pult", result="win")

    page = client.get("/").text

    copies = list(webapp.BACKUP_DIR.glob("matches-*.db"))
    assert len(copies) == 1
    assert [m.deck for m in storage.list_matches(db_path=copies[0])] == ["Pult"]
    assert (webapp.BACKUP_DIR / "voice" / "1-abc.webm").read_bytes() == b"spoken"
    assert "Backed up today" in page
    # the same day again: no second copy
    client.get("/")
    assert len(list(webapp.BACKUP_DIR.glob("matches-*.db"))) == 1


def test_decks_page_shows_card_usage_and_cards_missing_from_the_list(client: TestClient) -> None:
    text = (Path(__file__).parent / "data" / "battle_log.txt").read_text()
    # a game with an earlier list doesn't make the current list incomplete
    client.post("/matches", data={"deck": "Pult", "result": "win", "battle_log": text})
    storage.add_deck_version("Pult", "4 Dreepy TWM 128\n2 Budew PRE 4\n1 Iono PAL 185")
    assert "Not in this list" not in client.get("/decks").text
    client.post("/matches", data={"deck": "Pult", "result": "win", "battle_log": text})

    page = client.get("/decks").text

    assert "from 2 games with a battle log" in page
    assert "Not in this list, but in your deck according to the logs" in page
    assert "Buddy-Buddy Poffin" in page
    assert "never played in these games: Iono" in page
    # the list opened with the missing cards added, for review
    opened = client.get("/decks?add=Pult").text
    assert "1 Iono PAL 185\n1 Basic Darkness Energy" in opened
    match_id = max(m.id for m in storage.list_matches())
    assert "Update the list" in client.get(f"/matches/{match_id}").text


def test_a_guessed_opponent_name_is_not_passed_on(client: TestClient) -> None:
    text = (Path(__file__).parent / "data" / "battle_log.txt").read_text()
    data = {"deck": "Pult", "result": "win", "battle_log": text}
    client.post("/matches", data=data)
    first = storage.list_matches()[0]
    assert first.opponent_auto == 1

    # correcting it by hand makes it a name worth reusing
    client.put(f"/matches/{first.id}", json={"deck": "Pult", "opponent_deck": "Banette", "result": "win"})
    assert storage.get_match(first.id).opponent_auto == 0
    client.post("/matches", data=data)
    assert max(storage.list_matches(), key=lambda m: m.id).opponent_deck == "Banette"


def test_trimming_moves_the_log_times_along(client: TestClient) -> None:
    match_id = storage.log_match(deck="Pult", result="win")
    storage.set_log_times(match_id, {"2-1": 95.0, "2-4": 20.0})

    storage.shift_offsets(match_id, 60.0)

    assert json.loads(storage.get_match(match_id).log_times) == {"2-1": 35.0, "2-4": 0.0}
    assert client.get(f"/matches/{match_id}/timeline").json()["log_times"] == {"2-1": 35.0, "2-4": 0.0}


def test_stats_show_game_length_from_logs_and_markers(client: TestClient) -> None:
    text = (Path(__file__).parent / "data" / "battle_log.txt").read_text()
    client.post("/matches", data={"deck": "Pult", "result": "win", "battle_log": text})  # 4 turns
    marked = storage.log_match(deck="Pult", result="loss")
    storage.replace_turns(marked, [(float(i * 60), "you") for i in range(10)])  # 10 markers
    storage.log_match(deck="Slob", result="loss")  # length unknown: not counted

    page = client.get("/stats").text
    section = page[page.index("Game length") : page.index("Opening hands")]

    assert "All games" in section and ">7.0<" in section  # (4 + 10) / 2
    assert "With Pult" in section and "With Slob" not in section


def _log_text() -> str:
    return (Path(__file__).parent / "data" / "battle_log.txt").read_text()


def test_opponents_page_gathers_record_cards_and_note(client: TestClient) -> None:
    client.post("/matches", data={"deck": "Pult", "result": "win", "battle_log": _log_text()})
    storage.log_match(deck="Pult", opponent_deck="Dhelmise / Poltchageist", result="loss")
    client.put("/matchups/note", json={"opponent_deck": "Dhelmise / Poltchageist", "note": "they stall"})

    page = client.get("/opponents").text

    assert "Dhelmise / Poltchageist" in page
    assert "1&ndash;1&ndash;0" in page
    assert "they stall" in page
    assert "Ultra Ball" in page  # what the opponent played, from the log


def test_weeks_page_lists_lessons_and_misplays(client: TestClient) -> None:
    match_id = storage.log_match(deck="Pult", opponent_deck="Tauros", result="loss")
    storage.set_lesson(match_id, "count prizes first")
    storage.add_note(match_id, "benched too much", offset_seconds=30.0, label="misplay")

    page = client.get("/weeks").text

    assert "0&ndash;1&ndash;0" in page
    assert "count prizes first" in page
    assert "benched too much" in page and "1 Misplay" in page
    assert f"/matches/{match_id}?t=30.00" in page


def test_export_puts_comments_and_notes_where_they_belong(client: TestClient) -> None:
    client.post(
        "/matches",
        data={"deck": "Pult", "opponent_deck": "Banette", "result": "win", "battle_log": _log_text()},
    )
    match_id = storage.list_matches()[0].id
    storage.set_lesson(match_id, "bench Meowth earlier")
    storage.add_note(match_id, "Poffin first", label="misplay", log_turn=2, log_action=1)
    storage.replace_turns(match_id, [(60.0, "opponent"), (90.0, "you"), (150.0, "opponent"), (170.0, "you")])
    storage.add_note(match_id, "slow here", offset_seconds=100.0)

    resp = client.get(f"/matches/{match_id}/export")

    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment" in resp.headers["content-disposition"]
    text = resp.text
    assert text.startswith("# Pult vs Banette — win")
    assert "**Lesson:** bench Meowth earlier" in text
    assert "## Turn 2 — you (1:30)" in text
    turn_two = text[text.index("## Turn 2") : text.index("## Turn 3")]
    assert "2. Played Buddy-Buddy Poffin.\n   - Drew 2 cards" in turn_two
    assert "   > [Misplay] Poffin first" in turn_two
    assert "- Note at 1:40: slow here" in turn_two


def test_spoken_comment_on_a_log_line(client: TestClient) -> None:
    match_id = storage.log_match(deck="Pult", result="win")

    note = client.post(
        f"/matches/{match_id}/voice",
        files={"audio": ("note", b"opus", "audio/webm")},
        data={"log_turn": "3", "log_action": "1", "label": "key"},
    ).json()

    assert (note["log_turn"], note["log_action"], note["label"]) == (3, 1, "key")
    assert note["offset_seconds"] is None and note["audio_url"]


def test_stats_show_first_prize_setup_and_turn_activity(client: TestClient) -> None:
    client.post("/matches", data={"deck": "Pult", "result": "win", "battle_log": _log_text()})

    page = client.get("/stats").text
    section = page[page.index("How your games go") :]

    assert "Nobody (game ended first)" in section  # no Prize card was taken in this log
    assert "Drakloak" in section and "Dudunsparce" in section  # what I evolved into
    assert "Without attaching an Energy" in section


def test_lucky_topdeck_and_note_labels(client: TestClient) -> None:
    # with a recording, so the page has the label buttons
    match_id = storage.log_match(deck="Pult", result="win", video_file="recordings/x.mp4")

    for label in ("topdeck", "note"):
        resp = client.post(f"/matches/{match_id}/notes", json={"label": label, "offset_seconds": 5.0})
        assert resp.status_code == 200 and resp.json()["label"] == label

    page = client.get(f"/matches/{match_id}").text
    assert 'data-label="topdeck"' in page and "Lucky topdeck" in page
    assert 'data-label="note"' in page
    assert "Lucky topdeck" in client.get("/moments?label=topdeck").text


COMEBACK = """Setup
Ash drew 7 cards for the opening hand.
- 7 drawn cards.
   • Pikachu, Iono

Gary's Turn
Gary took 2 Prize cards.

Ash's Turn
Ash played Iono.
Ash took 2 Prize cards.

Gary's Turn
Gary drew a card.

Ash's Turn
Ash took 2 Prize cards.
Ash took 2 Prize cards.
Gary conceded. Ash wins.
"""


def test_comeback_shows_on_the_list_the_match_and_the_records(client: TestClient) -> None:
    client.post("/matches", data={"deck": "Pult", "result": "win", "battle_log": COMEBACK})
    match_id = storage.list_matches()[0].id

    assert "comeback" in client.get("/").text
    assert "You won this after being 2 Prize cards behind" in client.get(f"/matches/{match_id}").text
    stats = client.get("/stats").text
    records = stats[stats.index("<h2>Records</h2>") : stats.index("<h2>By deck</h2>")]
    assert "Biggest comeback" in records and "won from 2 Prize cards behind" in records
    assert "Fastest win" in records and "4 turns" in records
    assert "1 comeback, 0 lost from ahead" in records
    assert "Most played card" in records and "Iono" in records


def test_start_page_shows_form_session_and_an_older_lesson(client: TestClient) -> None:
    for index in range(5):
        match_id = storage.log_match(deck="Pult", result="win" if index else "loss")
        storage.set_lesson(match_id, f"lesson {index}")

    page = client.get("/").text

    assert "Last 5:" in page
    assert "4 wins in a row" in page
    assert "this session: 4&ndash;1&ndash;0" in page  # all logged just now
    # the three newest, and one of the two older ones brought back
    for index in (2, 3, 4):
        assert f"lesson {index}" in page
    older = page[page.index("From further back:") :]
    assert "lesson 0" in older or "lesson 1" in older


def test_misplay_words_and_recent_opponents(client: TestClient) -> None:
    match_id = storage.log_match(deck="Pult", opponent_deck="Tauros", result="loss")
    storage.log_match(deck="Pult", opponent_deck="Tauros", result="win")
    storage.log_match(deck="Pult", opponent_deck="Alakazam", result="loss")
    storage.add_note(match_id, "too slow with the retreat", offset_seconds=5.0, label="misplay")
    storage.add_note(match_id, "retreat came a turn late", offset_seconds=9.0, label="misplay")

    moments = client.get("/moments").text
    assert "Words that keep coming back" in moments and "retreat &times;2" in moments
    assert "Words that keep coming back" not in client.get("/moments?label=good").text

    opponents = client.get("/opponents").text
    meta = opponents[opponents.index("What you have been running into") : opponents.index("<h2>Per deck</h2>")]
    assert meta.index("Tauros") < meta.index("Alakazam")  # met most, listed first
    assert "67%" in meta

    weeks = client.get("/weeks").text
    assert "Sessions" in weeks and "over 3 games" in weeks
