from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pokemontcg_analyser import storage, webapp


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Static video serving is mounted once at import time (real recordings/
    # dir), so these tests only exercise routes/data, not actual file
    # serving — swapping the db is enough for that.
    monkeypatch.setattr(storage, "DEFAULT_DB_PATH", tmp_path / "matches.db")
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
