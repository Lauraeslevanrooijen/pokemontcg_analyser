"""Local review app: watch a recorded match and drop timestamped notes on it.

Runs on 127.0.0.1 only — this serves your own recorded gameplay, no reason
to expose it beyond localhost. `/media` is scoped to the recordings folder
specifically (not the whole project root), so the server never exposes
data/matches.db or source over HTTP.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import analysis, storage

TEMPLATES_DIR = Path(__file__).parent / "templates"
RECORDINGS_DIR = Path("recordings")

app = FastAPI(title="Pokémon TCG Live analyser")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

RECORDINGS_DIR.mkdir(exist_ok=True)
app.mount("/media", StaticFiles(directory=str(RECORDINGS_DIR)), name="media")


def db_path() -> Path:
    """Overridable in tests via app.dependency_overrides is unnecessary here
    since routes call this directly; tests monkeypatch storage.DEFAULT_DB_PATH."""
    return storage.DEFAULT_DB_PATH


@app.get("/")
def index(request: Request):
    matches = storage.list_matches(db_path=db_path())
    return templates.TemplateResponse(
        request, "index.html", {"matches": matches}
    )


@app.post("/matches")
def create_match(
    deck: str = Form(...),
    opponent_deck: str = Form(""),
    result: storage.Result = Form(...),
    notes: str = Form(""),
):
    storage.log_match(
        deck=deck,
        result=result,
        opponent_deck=opponent_deck or None,
        notes=notes or None,
        db_path=db_path(),
    )
    return RedirectResponse("/", status_code=303)


@app.get("/matches/{match_id}")
def match_detail(request: Request, match_id: int):
    match = storage.get_match(match_id, db_path=db_path())
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    events = storage.list_events(match_id, db_path=db_path())
    video_filename = Path(match.video_file).name if match.video_file else None
    return templates.TemplateResponse(
        request,
        "match.html",
        {"match": match, "events": events, "video_filename": video_filename},
    )


@app.post("/matches/{match_id}/analyze")
def analyze_match(match_id: int):
    match = storage.get_match(match_id, db_path=db_path())
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    if not match.video_file:
        raise HTTPException(status_code=400, detail="Match has no recording")
    changes = analysis.detect_scene_changes(Path(match.video_file))
    storage.add_events(
        match_id,
        [(c.offset_seconds, "scene_change", f"score={c.score:.1f}") for c in changes],
        db_path=db_path(),
    )
    return RedirectResponse(f"/matches/{match_id}", status_code=303)


class NoteIn(BaseModel):
    text: str
    offset_seconds: float | None = None


@app.post("/matches/{match_id}/notes")
def add_note(match_id: int, note: NoteIn):
    match = storage.get_match(match_id, db_path=db_path())
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    event_id = storage.add_note(
        match_id, note.text, offset_seconds=note.offset_seconds, db_path=db_path()
    )
    return {
        "id": event_id,
        "offset_seconds": note.offset_seconds,
        "kind": "note",
        "detail": note.text,
    }
