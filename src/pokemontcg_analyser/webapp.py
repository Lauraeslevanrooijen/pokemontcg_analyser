"""Local review app: record a match, watch it back and drop timestamped
notes on it.

Runs on 127.0.0.1 only — this serves your own recorded gameplay, no reason
to expose it beyond localhost. `/media` is scoped to the recordings folder
specifically (not the whole project root), so the server never exposes
data/matches.db or source over HTTP.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import battlelog, cards, decks, insights, recorder, storage, transcribe, turns

TEMPLATES_DIR = Path(__file__).parent / "templates"
RECORDINGS_DIR = Path("recordings")
VOICE_DIRNAME = "voice"
# Untrimmed recordings set aside by trimming are deleted after this long.
ORIGINALS_RETENTION_DAYS = 14
MAX_VOICE_NOTE_BYTES = 25 * 1024 * 1024
# What browsers' MediaRecorder produces, by container.
VOICE_EXTENSIONS = {"audio/webm": "webm", "audio/mp4": "m4a", "audio/ogg": "ogg"}

# The one recording in progress, if any. Screen capture is a single shared
# resource and this app is a single local process, so module state is enough.
_recording: recorder.Recording | None = None
# Why the last attempt to start a recording failed, shown on the start page
# until the next attempt: {"permission": bool, "detail": str}.
_recording_problem: dict | None = None


# Spoken notes waiting to be written out. One worker, so the speech model is
# loaded once and notes are done in the order they were recorded.
_transcriptions: queue.Queue[int] = queue.Queue()
_transcribing: set[int] = set()
_transcriber: threading.Thread | None = None


def _transcription_vocabulary() -> list[str]:
    """Card names from the saved decklists (newest list first), then the
    deck names matches were logged under."""
    names: list[str] = []
    for version in reversed(storage.list_deck_versions(db_path=db_path())):
        names += [version.deck, *decks.card_names(version.decklist)]
    for match in storage.list_matches(db_path=db_path()):
        names += [match.deck, match.opponent_deck or ""]
    return list(dict.fromkeys(name for name in names if name))


def _transcribe_worker() -> None:
    while True:
        event_id = _transcriptions.get()
        try:
            event = storage.get_event(event_id, db_path=db_path())
            if event is None or not event.audio_file:
                continue
            text = transcribe.transcribe(
                RECORDINGS_DIR / VOICE_DIRNAME / event.audio_file, _transcription_vocabulary()
            )
            # The note may have been typed over or deleted in the meantime.
            event = storage.get_event(event_id, db_path=db_path())
            if text and event is not None and not event.detail:
                storage.set_event_detail(event_id, text, db_path=db_path())
        except Exception:
            # A failed transcription just leaves the note without text.
            pass
        finally:
            _transcribing.discard(event_id)


def queue_transcription(event_id: int) -> None:
    global _transcriber
    _transcribing.add(event_id)
    _transcriptions.put(event_id)
    if _transcriber is None or not _transcriber.is_alive():
        _transcriber = threading.Thread(target=_transcribe_worker, daemon=True)
        _transcriber.start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    recorder.purge_originals(RECORDINGS_DIR, ORIGINALS_RETENTION_DAYS)
    # Compiling the recorder takes ~20s the first time; do it now, out of
    # the way, so it is ready when a recording is started.
    threading.Thread(target=recorder.build_capture_helper, daemon=True).start()
    yield
    # Don't leave an unfinalized (unplayable) MP4 behind if the server is
    # stopped mid-recording.
    if _recording is not None:
        _recording.stop()


app = FastAPI(title="Pokémon TCG Live analyser", lifespan=lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

RECORDINGS_DIR.mkdir(exist_ok=True)
app.mount("/media", StaticFiles(directory=str(RECORDINGS_DIR)), name="media")


def db_path() -> Path:
    """Overridable in tests via app.dependency_overrides is unnecessary here
    since routes call this directly; tests monkeypatch storage.DEFAULT_DB_PATH."""
    return storage.DEFAULT_DB_PATH


def capture_devices() -> tuple[list[recorder.CaptureDevice], list[recorder.CaptureDevice]]:
    """The screens and microphones available right now. Not cached: device
    indexes shift whenever a camera or microphone (an iPhone, a headset)
    connects, and a stale index would record the wrong thing."""
    try:
        return recorder.screens_and_microphones()
    except recorder.FfmpegNotFoundError:
        return [], []


def active_recording() -> recorder.Recording | None:
    return _recording if _recording is not None and _recording.is_running else None


def unlogged_recordings(matches: list[storage.Match]) -> list[str]:
    """Recordings no match points at yet, newest first."""
    logged = {Path(m.video_file).name for m in matches if m.video_file}
    active = active_recording()
    if active is not None:
        logged.add(active.video_path.name)
    names = [p.name for p in RECORDINGS_DIR.glob("*.mp4") if p.name not in logged]
    return sorted(names, reverse=True)


def format_size(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:.1f} GB"
    return f"{size / 1024**2:.0f} MB"


def matches_matching(
    matches: list[storage.Match], q: str, result: str, deck: str
) -> list[storage.Match]:
    """Filter by result and deck, and search the free text of a match:
    deck names, notes, lesson and everything written on its timeline."""
    needle = q.strip().lower()
    note_text: dict[int, str] = {}
    if needle:
        for event in storage.list_all_events(db_path=db_path()):
            # A detected turn's detail is whose turn it is, not a note.
            if event.detail and event.kind != "turn":
                note_text[event.match_id] = note_text.get(event.match_id, "") + "\n" + event.detail
    found = []
    for m in matches:
        if result and m.result != result:
            continue
        if deck and m.deck != deck:
            continue
        haystack = "\n".join(
            [
                m.deck,
                m.opponent_deck or "",
                m.notes or "",
                m.lesson or "",
                m.battle_log or "",
                note_text.get(m.id, ""),
            ]
        ).lower()
        if needle and needle not in haystack:
            continue
        found.append(m)
    return found


@app.get("/")
def index(
    request: Request,
    video: str | None = None,
    q: str = "",
    result: str = "",
    deck: str = "",
):
    matches = storage.list_matches(db_path=db_path())
    recording = active_recording()
    screens, microphones = ([], []) if recording else capture_devices()
    sizes = {p.name: p.stat().st_size for p in RECORDINGS_DIR.glob("*.mp4")}
    total_size = sum(p.stat().st_size for p in RECORDINGS_DIR.rglob("*") if p.is_file())
    # The app can stay open for days, so don't rely on startup alone.
    recorder.purge_originals(RECORDINGS_DIR, ORIGINALS_RETENTION_DAYS)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "matches": matches_matching(matches, q, result, deck),
            "total_matches": len(matches),
            "filters": {"q": q, "result": result, "deck": deck},
            "my_decks": sorted({m.deck for m in matches}),
            "lessons": [m for m in matches if m.lesson][:3],
            "video_sizes": {
                m.id: format_size(sizes[Path(m.video_file).name])
                for m in matches
                if m.video_file and Path(m.video_file).name in sizes
            },
            "total_size": format_size(total_size) if total_size else None,
            "recording": recording,
            "recording_problem": _recording_problem,
            "marks": len(recorder.read_marks(recording.video_path)) if recording else 0,
            "devices": screens,
            "microphones": microphones,
            "unlogged": unlogged_recordings(matches),
            "selected_video": video,
            "known_decks": sorted(
                {d for m in matches for d in (m.deck, m.opponent_deck) if d}
            ),
        },
    )


@app.get("/stats")
def stats(request: Request):
    matches = storage.list_matches(db_path=db_path())
    events = storage.list_all_events(db_path=db_path())
    hands = []
    for match in matches:
        if not match.battle_log:
            continue
        log = battlelog.parse(match.battle_log)
        hand = log.opening_hand()
        if hand:
            hands.append((match.result, hand, _card_kinds(_deck_cards(match))))
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "stats": insights.build(matches, events),
            "labels": storage.LABELS,
            "openings": insights.openings(hands),
        },
    )


@app.get("/decks")
def decks_page(request: Request):
    matches = storage.list_matches(db_path=db_path())
    versions = storage.list_deck_versions(db_path=db_path())
    names = sorted({m.deck for m in matches} | {v.deck for v in versions}, key=str.lower)
    deck_views = []
    for name in names:
        deck_matches = [m for m in matches if m.deck == name]
        rows = []
        previous = None
        for number, version in enumerate((v for v in versions if v.deck == name), start=1):
            record = insights.Record()
            for m in deck_matches:
                if m.deck_version_id == version.id:
                    record.add(m.result)
            rows.append(
                {
                    "number": number,
                    "version": version,
                    "record": record,
                    "cards": decks.card_count(version.decklist),
                    "sections": decks.sections(version.decklist),
                    "changes": decks.diff(previous.decklist, version.decklist) if previous else [],
                }
            )
            previous = version
        unversioned = insights.Record()
        overall = insights.Record()
        for m in deck_matches:
            overall.add(m.result)
            if m.deck_version_id is None:
                unversioned.add(m.result)
        deck_views.append(
            {
                "name": name,
                "overall": overall,
                "unversioned": unversioned,
                "versions": list(reversed(rows)),  # newest first
                "current": previous,
            }
        )
    return templates.TemplateResponse(request, "decks.html", {"decks": deck_views})


@app.get("/cards/image/{set_code}/{number}")
def card_image(set_code: str, number: str):
    """A card's picture by its decklist printing ("TWM", "130"), fetched
    from TCGdex the first time and served from disk after that."""
    if not (set_code.isalnum() and number.isalnum()):
        raise HTTPException(status_code=404, detail="No such card")
    path = cards.image_path(set_code, number)
    if path is None:
        raise HTTPException(status_code=404, detail="No picture for this card")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "max-age=604800"})


@app.post("/decks")
def save_deck_version(
    deck: str = Form(...),
    decklist: str = Form(...),
    note: str = Form(""),
):
    deck, decklist = deck.strip(), decklist.strip()
    if not deck or not decklist:
        raise HTTPException(status_code=400, detail="A deck needs a name and a list")
    storage.add_deck_version(deck, decklist, note.strip() or None, db_path=db_path())
    return RedirectResponse("/decks", status_code=303)


@app.get("/moments")
def moments(request: Request, label: str = "misplay"):
    if label != "all" and label not in storage.LABELS:
        raise HTTPException(status_code=404, detail="Unknown label")
    matches = storage.list_matches(db_path=db_path())
    events = storage.list_all_events(db_path=db_path())
    return templates.TemplateResponse(
        request,
        "moments.html",
        {
            "moments": insights.moments(matches, events, None if label == "all" else label),
            "labels": storage.LABELS,
            "selected": label,
        },
    )


@app.post("/recording/start")
def start_recording(
    screen: str = Form(""),
    voice: bool = Form(False),
    microphone: str = Form(""),
):
    """Devices are chosen by name and looked up again here, so the choice
    still means the same device if the indexes moved since the page loaded."""
    global _recording, _recording_problem
    _recording_problem = None
    if active_recording() is not None:
        raise HTTPException(status_code=409, detail="Already recording")
    screens, microphones = capture_devices()
    if not screens:
        raise HTTPException(status_code=400, detail="No screen to record found")
    device = next((d for d in screens if d.name == screen), screens[0])
    audio_index = audio_name = None
    if voice:
        chosen = next((d for d in microphones if d.name == microphone), None)
        if chosen is None:
            raise HTTPException(status_code=400, detail="That microphone is not connected")
        audio_index, audio_name = chosen.index, chosen.name
    # The window position is relative to the main display, so only crop
    # to the game when that is the screen being recorded.
    main_display = device.name == "Capture screen 0"
    crop = recorder.game_crop() if main_display else None
    try:
        _recording = recorder.start_recording(
            device.index,
            RECORDINGS_DIR,
            audio_index=audio_index,
            crop=crop,
            audio_name=audio_name,
            main_display=main_display,
        )
    except recorder.ScreenRecordingPermissionError as exc:
        _recording_problem = {"permission": True, "detail": str(exc)}
    except (RuntimeError, OSError) as exc:
        _recording_problem = {"permission": False, "detail": str(exc)}
    # Back to the start page either way; it shows what went wrong.
    return RedirectResponse("/", status_code=303)


@app.post("/recording/open-settings")
def open_screen_recording_settings():
    """Open the macOS settings pane where Screen Recording is allowed."""
    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"],
        capture_output=True,
    )
    return RedirectResponse("/", status_code=303)


@app.post("/recording/mark")
def mark_recording():
    recording = active_recording()
    if recording is None:
        raise HTTPException(status_code=409, detail="Not recording")
    return {"count": len(recording.mark())}


@app.post("/recording/stop")
def stop_recording():
    global _recording
    if _recording is None:
        raise HTTPException(status_code=409, detail="Not recording")
    video_path = _recording.stop()
    _recording = None
    return RedirectResponse(f"/?video={video_path.name}", status_code=303)


@app.post("/matches")
def create_match(
    deck: str = Form(...),
    opponent_deck: str = Form(""),
    result: storage.Result = Form(...),
    notes: str = Form(""),
    video_file: str = Form(""),
    battle_log: str = Form(""),
):
    video_path = None
    if video_file:
        # Only a bare filename inside the recordings folder is accepted.
        video_path = RECORDINGS_DIR / Path(video_file).name
        if not video_path.is_file():
            raise HTTPException(status_code=400, detail="Unknown recording")
    # A pasted battle log knows who won and who went first, so it overrules
    # the result picked in the form.
    log = battlelog.parse(battle_log) if battle_log.strip() else None
    if log is not None and not log.turns:
        raise HTTPException(
            status_code=400, detail="This doesn't look like a battle log: no turns found in it"
        )
    if log is not None and log.result:
        result = log.result
    match_id = storage.log_match(
        deck=deck,
        result=result,
        opponent_deck=opponent_deck or None,
        notes=notes or None,
        video_file=str(video_path) if video_path else None,
        db_path=db_path(),
    )
    if log is not None:
        _apply_battle_log(match_id, battle_log.strip(), log)
    if video_path:
        storage.add_events(
            match_id,
            [(offset, "mark", None) for offset in recorder.read_marks(video_path)],
            db_path=db_path(),
        )
        detect_turns_in_background(match_id)
    # With a recording attached the next thing to do is watch it back.
    return RedirectResponse(
        f"/matches/{match_id}" if video_path or log is not None else "/", status_code=303
    )


@app.get("/matches/{match_id}")
def match_detail(request: Request, match_id: int):
    match = storage.get_match(match_id, db_path=db_path())
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    events = storage.list_events(match_id, db_path=db_path())
    video_filename = Path(match.video_file).name if match.video_file else None
    # Trimming rewrites the file under the same name; the version in the URL
    # keeps the browser from playing its cached copy of the old one.
    video_version = 0
    video_size = None
    if video_filename and (RECORDINGS_DIR / video_filename).is_file():
        video_stat = (RECORDINGS_DIR / video_filename).stat()
        video_version = int(video_stat.st_mtime)
        video_size = format_size(video_stat.st_size)
    return templates.TemplateResponse(
        request,
        "match.html",
        {
            "match": match,
            "events": [_event_json(e) for e in events],
            "labels": storage.LABELS,
            "video_filename": video_filename,
            "video_version": video_version,
            "video_size": video_size,
            "retention_days": ORIGINALS_RETENTION_DAYS,
            "can_transcribe": transcribe.available(),
            "battle_log": _battle_log_json(match.battle_log, match),
            "detecting_turns": match_id in _detecting,
        },
    )


def _deck_cards(match: storage.Match) -> dict[str, decks.ListedCard]:
    """The cards of the list a match was played with, by name. Falls back
    to the deck's newest list for matches logged before one was saved."""
    versions = [v for v in storage.list_deck_versions(db_path=db_path()) if v.deck == match.deck]
    version = next((v for v in versions if v.id == match.deck_version_id), None)
    version = version or (versions[-1] if versions else None)
    if version is None:
        return {}
    return {
        card.name: card
        for section in decks.sections(version.decklist)
        for card in section.cards
    }


def _card_kinds(listed: dict[str, decks.ListedCard]) -> dict[str, str]:
    """What kind each listed card is, from the card database."""
    kinds = {}
    for name, card in listed.items():
        found = cards.lookup(card.set_code, card.number) if card.printing else None
        if found is not None and found.kind:
            kinds[name] = found.kind
    return kinds


def _opponent_deck_name(log: battlelog.BattleLog, match_id: int) -> str | None:
    """A name for the opponent's deck. If an earlier match against the same
    Pokémon was given a name by hand, that name is used again, so the
    matchup stats keep counting it as one deck."""
    suggested = log.deck_name(log.opponent)
    if suggested is None:
        return None
    for other in storage.list_matches(db_path=db_path()):
        if other.id == match_id or not other.battle_log or not other.opponent_deck:
            continue
        earlier = battlelog.parse(other.battle_log)
        if earlier.deck_name(earlier.opponent) == suggested:
            return other.opponent_deck
    return suggested


def _apply_battle_log(match_id: int, text: str, log: battlelog.BattleLog) -> None:
    """Store a log and take from it what it settles: who went first, the
    result, and a name for the opponent's deck if none was given."""
    storage.set_battle_log(match_id, text, db_path=db_path())
    if log.turn_order:
        storage.set_turn_order(match_id, log.turn_order, db_path=db_path())
    if log.result:
        storage.set_result(match_id, log.result, db_path=db_path())
    match = storage.get_match(match_id, db_path=db_path())
    if match is not None and not match.opponent_deck:
        name = _opponent_deck_name(log, match_id)
        if name:
            storage.set_opponent_deck(match_id, name, db_path=db_path())


def _battle_log_json(text: str | None, match: storage.Match | None = None) -> dict | None:
    """A stored battle log, parsed and worded for the timeline."""
    if not text:
        return None
    log = battlelog.parse(text)
    listed = _deck_cards(match) if match is not None else {}

    def actions(items: list[battlelog.Action]) -> list[dict]:
        return [{"text": a.text, "details": a.details} for a in items]

    return {
        "setup": actions(log.narrated(battlelog.LogTurn(player="", actions=log.setup))),
        "turns": [
            {
                "owner": ("you" if turn.player == log.me else "opponent") if log.me else None,
                "actions": actions(log.narrated(turn)),
            }
            for turn in log.turns
        ],
        "opponent_pokemon": log.pokemon_of(log.opponent),
        "prize_race": log.prize_race(),
        "opening_hand": [
            {
                "name": name,
                "image": (
                    f"/cards/image/{listed[name].set_code}/{listed[name].number}"
                    if name in listed and listed[name].printing
                    else None
                ),
            }
            for name in log.opening_hand()
        ],
    }


def _event_json(event: storage.Event) -> dict:
    return {
        "id": event.id,
        "kind": event.kind,
        "offset_seconds": event.video_offset_seconds,
        "detail": event.detail,
        "label": event.label,
        "audio_url": f"/media/{VOICE_DIRNAME}/{event.audio_file}" if event.audio_file else None,
        "transcribing": event.id in _transcribing,
    }


def _require_match(match_id: int) -> storage.Match:
    match = storage.get_match(match_id, db_path=db_path())
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    return match


def _require_event(event_id: int) -> storage.Event:
    event = storage.get_event(event_id, db_path=db_path())
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


class TurnOrderIn(BaseModel):
    turn_order: storage.TurnOrder | None = None


@app.put("/matches/{match_id}/turn-order")
def set_turn_order(match_id: int, body: TurnOrderIn):
    _require_match(match_id)
    storage.set_turn_order(match_id, body.turn_order, db_path=db_path())
    return {"turn_order": body.turn_order}


class BattleLogIn(BaseModel):
    text: str = ""


@app.put("/matches/{match_id}/battle-log")
def set_battle_log(match_id: int, body: BattleLogIn):
    """Attach the battle log copied from the game (or remove it, with empty
    text). The log settles who went first and who won, so those are taken
    from it."""
    match = _require_match(match_id)
    text = body.text.strip()
    if not text:
        storage.set_battle_log(match_id, None, db_path=db_path())
        return {"battle_log": None, "turn_order": match.turn_order, "result": match.result}
    log = battlelog.parse(text)
    if not log.turns:
        raise HTTPException(
            status_code=400, detail="This doesn't look like a battle log: no turns found in it"
        )
    _apply_battle_log(match_id, text, log)
    updated = _require_match(match_id)
    return {
        "battle_log": _battle_log_json(text, updated),
        "turn_order": updated.turn_order,
        "result": updated.result,
        "opponent_deck": updated.opponent_deck,
        "result_changed": bool(log.result and log.result != match.result),
    }


class LessonIn(BaseModel):
    lesson: str = ""


@app.put("/matches/{match_id}/lesson")
def set_lesson(match_id: int, body: LessonIn):
    _require_match(match_id)
    lesson = body.lesson.strip()
    storage.set_lesson(match_id, lesson, db_path=db_path())
    return {"lesson": lesson or None}


@app.post("/matches/{match_id}/recording/delete")
def delete_recording(match_id: int):
    """Delete a match's video to free disk space; its notes are kept."""
    match = _require_match(match_id)
    if not match.video_file:
        raise HTTPException(status_code=400, detail="Match has no recording")
    name = Path(match.video_file).name
    active = active_recording()
    if active is not None and active.video_path.name == name:
        raise HTTPException(status_code=409, detail="Still recording this video")
    video_path = RECORDINGS_DIR / name
    for path in (
        video_path,
        video_path.with_suffix(".json"),
        video_path.with_suffix(".log"),
        RECORDINGS_DIR / recorder.ORIGINALS_DIRNAME / name,
    ):
        path.unlink(missing_ok=True)
    storage.clear_video(match_id, db_path=db_path())
    return RedirectResponse(f"/matches/{match_id}", status_code=303)


class GameStartIn(BaseModel):
    offset_seconds: float | None = None


@app.put("/matches/{match_id}/game-start")
def set_game_start(match_id: int, body: GameStartIn):
    _require_match(match_id)
    storage.set_game_start(match_id, body.offset_seconds, db_path=db_path())
    return {"game_start_seconds": body.offset_seconds}


@app.post("/matches/{match_id}/trim")
def trim_to_game_start(match_id: int):
    """Cut the part of the recording before the game start off the file."""
    match = _require_match(match_id)
    if not match.video_file or match.game_start_seconds is None:
        raise HTTPException(status_code=400, detail="Set the game start first")
    video_path = RECORDINGS_DIR / Path(match.video_file).name
    active = active_recording()
    if active is not None and active.video_path.name == video_path.name:
        raise HTTPException(status_code=409, detail="Still recording this video")
    with _video_lock:
        try:
            removed = recorder.trim_start(video_path, match.game_start_seconds)
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"Trimming failed: {exc}")
        storage.shift_offsets(match_id, removed, db_path=db_path())
    return {"removed_seconds": removed}


class NoteIn(BaseModel):
    text: str = ""
    offset_seconds: float | None = None
    label: storage.Label | None = None


class TurnIn(BaseModel):
    offset_seconds: float


@app.post("/matches/{match_id}/notes")
def add_note(match_id: int, note: NoteIn):
    _require_match(match_id)
    text = note.text.strip()
    if not text and note.label is None:
        raise HTTPException(status_code=400, detail="A note needs text or a label")
    event_id = storage.add_note(
        match_id,
        text,
        offset_seconds=note.offset_seconds,
        label=note.label,
        db_path=db_path(),
    )
    return _event_json(_require_event(event_id))


@app.post("/matches/{match_id}/voice")
async def add_voice_note(
    match_id: int,
    audio: UploadFile = File(...),
    offset_seconds: float | None = Form(None),
):
    """Save a spoken note recorded in the browser."""
    _require_match(match_id)
    container = (audio.content_type or "").split(";")[0].strip().lower()
    extension = VOICE_EXTENSIONS.get(container)
    if extension is None:
        raise HTTPException(status_code=400, detail="Not an audio recording")
    data = await audio.read(MAX_VOICE_NOTE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty recording")
    if len(data) > MAX_VOICE_NOTE_BYTES:
        raise HTTPException(status_code=413, detail="Voice note too long")

    voice_dir = RECORDINGS_DIR / VOICE_DIRNAME
    voice_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{match_id}-{uuid.uuid4().hex[:12]}.{extension}"
    (voice_dir / filename).write_bytes(data)
    event_id = storage.add_event(
        match_id,
        "note",
        offset_seconds=offset_seconds,
        audio_file=filename,
        db_path=db_path(),
    )
    if transcribe.available():
        queue_transcription(event_id)
    return _event_json(_require_event(event_id))


@app.get("/events/{event_id}")
def get_event(event_id: int):
    # Asked before the event is read, for the same reason as in
    # _timeline_json: "done" must describe the text that is returned.
    transcribing = event_id in _transcribing
    return {**_event_json(_require_event(event_id)), "transcribing": transcribing}


@app.post("/events/{event_id}/transcribe")
def transcribe_event(event_id: int):
    """Write out a spoken note that has no text yet."""
    event = _require_event(event_id)
    if not event.audio_file:
        raise HTTPException(status_code=400, detail="Not a voice note")
    if not transcribe.available():
        raise HTTPException(status_code=503, detail="Transcription is not installed")
    if event_id not in _transcribing:
        queue_transcription(event_id)
    return _event_json(event)


# Matches whose recording is being searched for turns in the background,
# with how many searches are running: counted, so one search finishing
# doesn't report a match as done while another is still going.
_detecting: dict[int, int] = {}
_detecting_lock = threading.Lock()
# Held while a recording is read for turns or rewritten by trimming, so a
# trim never moves the video out from under a detection.
_video_lock = threading.Lock()


def _store_detected_turns(match_id: int, replace: bool) -> list[turns.Turn]:
    """Find the turns in a match's recording and put them on its timeline.
    With `replace` off, markers that are already there are left alone."""
    with _video_lock:
        match = storage.get_match(match_id, db_path=db_path())
        if match is None or not match.video_file:
            return []
        found = turns.detect_turns(RECORDINGS_DIR / Path(match.video_file).name)
        if not found:
            return []
        # Reading the video takes a while; look at the match as it is now.
        match = storage.get_match(match_id, db_path=db_path())
        if match is None:
            return []
        has_markers = any(e.kind == "turn" for e in storage.list_events(match_id, db_path=db_path()))
        if replace or not has_markers:
            storage.replace_turns(
                match_id, [(t.offset_seconds, t.owner) for t in found], db_path=db_path()
            )
            if match.turn_order is None:
                order = "first" if found[0].owner == "you" else "second"
                storage.set_turn_order(match_id, order, db_path=db_path())
        return found


def detect_turns_in_background(match_id: int) -> None:
    """Start looking for turns right away, so they are there (or nearly)
    by the time the review page is open."""

    def work() -> None:
        try:
            _store_detected_turns(match_id, replace=False)
        except Exception:
            # The button on the page still works; leave a trace in the log.
            traceback.print_exc()
        finally:
            with _detecting_lock:
                _detecting[match_id] -= 1
                if _detecting[match_id] <= 0:
                    del _detecting[match_id]

    with _detecting_lock:
        _detecting[match_id] = _detecting.get(match_id, 0) + 1
    threading.Thread(target=work, daemon=True).start()


def _timeline_json(match_id: int) -> dict:
    # Whether a search is still running is asked first: if it isn't, what
    # is read after this is the finished result, not a half-written one.
    detecting = match_id in _detecting
    match = _require_match(match_id)
    return {
        "events": [_event_json(e) for e in storage.list_events(match_id, db_path=db_path())],
        "turn_order": match.turn_order,
        "detecting": detecting,
    }


@app.get("/matches/{match_id}/timeline")
def timeline(match_id: int):
    return _timeline_json(match_id)


@app.post("/matches/{match_id}/detect-turns")
def detect_turns(match_id: int):
    """Find the turn changes in the recording and replace the match's turn
    markers with them."""
    match = _require_match(match_id)
    if not match.video_file:
        raise HTTPException(status_code=400, detail="Match has no recording")
    try:
        found = _store_detected_turns(match_id, replace=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not read the recording: {exc}")
    if not found:
        raise HTTPException(status_code=422, detail="No turns found in this recording")
    return _timeline_json(match_id)


@app.post("/matches/{match_id}/turns")
def add_turn(match_id: int, turn: TurnIn):
    _require_match(match_id)
    event_id = storage.add_event(
        match_id, "turn", offset_seconds=turn.offset_seconds, db_path=db_path()
    )
    return _event_json(_require_event(event_id))


@app.patch("/events/{event_id}")
def edit_event(event_id: int, note: NoteIn):
    event = _require_event(event_id)
    if event.kind == "turn":
        raise HTTPException(status_code=400, detail="Turn markers have no text")
    text = note.text.strip()
    if not text and note.label is None and not event.audio_file:
        raise HTTPException(status_code=400, detail="A note needs text or a label")
    storage.update_note(event_id, text, note.label, db_path=db_path())
    return _event_json(_require_event(event_id))


@app.delete("/events/{event_id}")
def delete_event(event_id: int):
    event = _require_event(event_id)
    storage.delete_event(event_id, db_path=db_path())
    if event.audio_file:
        (RECORDINGS_DIR / VOICE_DIRNAME / Path(event.audio_file).name).unlink(missing_ok=True)
    return Response(status_code=204)
