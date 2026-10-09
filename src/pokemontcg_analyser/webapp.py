"""Local review app: record a match, watch it back and drop timestamped
notes on it.

Runs on 127.0.0.1 only — this serves your own recorded gameplay, no reason
to expose it beyond localhost. `/media` is scoped to the recordings folder
specifically (not the whole project root), so the server never exposes
data/matches.db or source over HTTP.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import sqlite3
import subprocess
import threading
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import battlelog, cards, decks, insights, logtimes, recorder, storage, transcribe, turns

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


def _remove_recording(name: str) -> None:
    """Delete a recording and the files that belong to it."""
    video_path = RECORDINGS_DIR / name
    for path in (
        video_path,
        video_path.with_suffix(".json"),
        video_path.with_suffix(".log"),
        RECORDINGS_DIR / recorder.ORIGINALS_DIRNAME / name,
    ):
        path.unlink(missing_ok=True)


def recording_retention_weeks() -> int | None:
    value = storage.get_setting("recording_retention_weeks", db_path=db_path())
    return int(value) if value and value.isdigit() else None


def purge_old_recordings() -> int:
    """Delete the recordings of matches older than the chosen number of
    weeks; their logs and notes stay. Off until a number is chosen."""
    weeks = recording_retention_weeks()
    if weeks is None:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).isoformat()
    active = active_recording()
    removed = 0
    for match in storage.list_matches(db_path=db_path()):
        if not match.video_file or match.played_at_utc >= cutoff:
            continue
        name = Path(match.video_file).name
        if active is not None and active.video_path.name == name:
            continue
        _remove_recording(name)
        storage.clear_video(match.id, db_path=db_path())
        removed += 1
    return removed


# Where copies of the database and the spoken notes go. iCloud Drive when
# it is there, so a copy also exists off this laptop; otherwise Documents.
# POKEMONTCG_BACKUP_DIR picks another place.
_ICLOUD = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
BACKUP_DIR = Path(
    os.environ.get("POKEMONTCG_BACKUP_DIR")
    or (_ICLOUD if _ICLOUD.is_dir() else Path.home() / "Documents") / "Pokemon TCG Analyser backups"
)
BACKUPS_KEPT = 14


def run_backup() -> Path | None:
    """Once a day: a dated copy of the database, and any spoken notes not
    copied yet. Recordings are too big to copy and are not included.
    Returns today's copy, or None if it could not be made."""
    today = BACKUP_DIR / f"matches-{datetime.now().strftime('%Y-%m-%d')}.db"
    try:
        if not today.exists():
            if not db_path().exists():
                return None
            storage.backup_database(today, db_path=db_path())
            for old in sorted(BACKUP_DIR.glob("matches-*.db"))[:-BACKUPS_KEPT]:
                old.unlink()
        voice_dir = RECORDINGS_DIR / VOICE_DIRNAME
        if voice_dir.is_dir():
            (BACKUP_DIR / VOICE_DIRNAME).mkdir(parents=True, exist_ok=True)
            for note in voice_dir.iterdir():
                copy = BACKUP_DIR / VOICE_DIRNAME / note.name
                if note.is_file() and not copy.exists():
                    shutil.copy2(note, copy)
    except (OSError, sqlite3.Error):
        return None
    return today


def _older_lesson(matches: list[storage.Match]) -> storage.Match | None:
    """One lesson from further back than the latest three, a different one
    each day, so old lessons come round again instead of scrolling away."""
    older = [m for m in matches if m.lesson][3:]
    if not older:
        return None
    return older[datetime.now().toordinal() % len(older)]


def _current_session(matches: list[storage.Match]) -> dict | None:
    """The sitting that is going on: the latest run of matches, if its last
    one was logged within the hour."""
    grouped = insights.sessions(matches)
    if not grouped:
        return None
    latest = grouped[0]
    last_played = datetime.fromisoformat(latest[-1].played_at_utc)
    if datetime.now(timezone.utc) - last_played > timedelta(minutes=60):
        return None
    record = insights.Record()
    for match in latest:
        record.add(match.result)
    return {"record": record, "games": len(latest)}


def _swings(matches: list[storage.Match]) -> dict[int, tuple[str, int]]:
    """Per match with a battle log: whether it was a comeback or a lost lead."""
    found = {}
    for match in matches:
        if not match.battle_log:
            continue
        swing = insights.prize_swing(match, battlelog.parse(match.battle_log).prize_race())
        if swing:
            found[match.id] = swing
    return found


def _records(matches: list[storage.Match], lengths: list[tuple[storage.Match, int]]) -> list[dict]:
    """Personal bests, each with the match it was set in (when it is one)."""
    records: list[dict] = []
    wins = [(m, turns) for m, turns in lengths if m.result == "win"]
    if wins:
        match, turns = min(wins, key=lambda item: item[1])
        records.append({"name": "Fastest win", "value": f"{turns} turns", "match": match})
    if lengths:
        match, turns = max(lengths, key=lambda item: item[1])
        records.append({"name": "Longest game", "value": f"{turns} turns", "match": match})
    swings = _swings(matches)
    by_id = {m.id: m for m in matches}
    comebacks = [(by_id[i], size) for i, (kind, size) in swings.items() if kind == "comeback"]
    if comebacks:
        match, size = max(comebacks, key=lambda item: item[1])
        records.append({"name": "Biggest comeback", "value": f"won from {size} Prize cards behind", "match": match})
    longest, current = insights.longest_streak(matches)
    if longest:
        records.append(
            {
                "name": "Longest winning streak",
                "value": f"{longest} {'game' if longest == 1 else 'games'}"
                + (" (still going)" if current == longest else ""),
                "match": None,
            }
        )
    played: dict[str, int] = {}
    for match in matches:
        if match.battle_log:
            log = battlelog.parse(match.battle_log)
            for card, times in log.cards_played(log.me).items():
                played[card] = played.get(card, 0) + times
    if played:
        card = max(played, key=lambda name: (played[name], name))
        records.append({"name": "Most played card", "value": f"{card}, {played[card]} times", "match": None})
    by_day: dict[str, int] = {}
    for match in matches:
        day = datetime.fromisoformat(match.played_at_utc).astimezone().strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0) + 1
    if by_day:
        day = max(by_day, key=lambda d: (by_day[d], d))
        records.append({"name": "Most games in a day", "value": f"{by_day[day]} on {day}", "match": None})
    return records


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
    if purge_old_recordings():
        matches = storage.list_matches(db_path=db_path())
    backup = run_backup()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "matches": matches_matching(matches, q, result, deck),
            "total_matches": len(matches),
            "filters": {"q": q, "result": result, "deck": deck},
            "my_decks": sorted({m.deck for m in matches}),
            "lessons": [m for m in matches if m.lesson][:3],
            "older_lesson": _older_lesson(matches),
            "form": [m.result for m in matches[:10]],
            "streak": insights.longest_streak(matches)[1],
            "session": _current_session(matches),
            "swings": _swings(matches),
            "video_sizes": {
                m.id: format_size(sizes[Path(m.video_file).name])
                for m in matches
                if m.video_file and Path(m.video_file).name in sizes
            },
            "total_size": format_size(total_size) if total_size else None,
            "retention_weeks": recording_retention_weeks(),
            "backup": backup,
            "backup_dir": BACKUP_DIR,
            "matchup_notes": storage.matchup_notes(db_path=db_path()),
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
    lengths = []
    markers: dict[int, int] = {}
    for event in events:
        if event.kind == "turn":
            markers[event.match_id] = markers.get(event.match_id, 0) + 1
    for match in matches:
        # The battle log knows exactly how many turns a game had; without
        # one, the turn markers on the recording are the next best count.
        log = battlelog.parse(match.battle_log) if match.battle_log else None
        turn_count = len(log.turns) if log is not None and log.turns else markers.get(match.id, 0)
        if turn_count:
            lengths.append((match, turn_count))
        if log is None:
            continue
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
            "tempo": insights.tempo(matches, events),
            "lengths": insights.game_lengths(lengths),
            "logs": insights.summarise_logs(_deck_log_facts(matches)),
            "records": _records(matches, lengths),
            "swing_counts": {
                kind: sum(1 for k, _ in _swings(matches).values() if k == kind)
                for kind in ("comeback", "lead lost")
            },
        },
    )


@app.get("/decks")
def decks_page(request: Request, add: str = ""):
    matches = storage.list_matches(db_path=db_path())
    versions = storage.list_deck_versions(db_path=db_path())
    names = sorted({m.deck for m in matches} | {v.deck for v in versions}, key=str.lower)
    deck_views = []
    for name in names:
        deck_matches = [m for m in matches if m.deck == name]
        deck_facts = _deck_log_facts(deck_matches)
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
                    "logs": insights.summarise_logs(
                        [f for f in deck_facts if f.match.deck_version_id == version.id]
                    ),
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
        # From the battle logs of this deck's matches: what gets played.
        logged = [(m, battlelog.parse(m.battle_log)) for m in deck_matches if m.battle_log]
        usage: dict[str, list[int]] = {}  # card -> [games played in, times played]
        seen: set[str] = set()
        for m, log in logged:
            # Only games played with the current list can show it to be
            # incomplete; older games had cards that have since been cut.
            if previous and m.deck_version_id == previous.id:
                seen |= log.cards_seen(log.me)
            for card, times in log.cards_played(log.me).items():
                entry = usage.setdefault(card, [0, 0])
                entry[0] += 1
                entry[1] += times
        listed = (
            {c.name for section in decks.sections(previous.decklist) for c in section.cards}
            if previous
            else set()
        )
        missing = sorted(seen - listed) if previous else []
        deck_views.append(
            {
                "name": name,
                "overall": overall,
                "unversioned": unversioned,
                "versions": list(reversed(rows)),  # newest first
                "current": previous,
                "logged_games": len(logged),
                "odds": (
                    decks.opening_odds(
                        previous.decklist,
                        _card_kinds(
                            {
                                c.name: c
                                for section in decks.sections(previous.decklist)
                                for c in section.cards
                            }
                        ),
                    )
                    if previous
                    else []
                ),
                "usage": sorted(usage.items(), key=lambda item: (-item[1][0], -item[1][1], item[0])),
                "never_played": sorted(listed - set(usage)) if logged else [],
                "missing": missing,
                # the current list with the missing cards added, ready to review
                "with_missing": (
                    previous.decklist.rstrip() + "\n" + "\n".join(f"1 {card}" for card in missing)
                    if previous and missing
                    else ""
                ),
            }
        )
    return templates.TemplateResponse(request, "decks.html", {"decks": deck_views, "add": add})


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


@app.get("/opponents")
def opponents(request: Request):
    """Everything known about each deck played against, in one place."""
    matches = storage.list_matches(db_path=db_path())
    notes = storage.matchup_notes(db_path=db_path())
    by_deck: dict[str, dict] = {}
    for match in matches:  # newest first
        if not match.opponent_deck:
            continue
        entry = by_deck.setdefault(
            match.opponent_deck,
            {"name": match.opponent_deck, "record": insights.Record(), "matches": [], "cards": {}, "logged": 0},
        )
        entry["record"].add(match.result)
        entry["matches"].append(match)
        if match.battle_log:
            log = battlelog.parse(match.battle_log)
            entry["logged"] += 1
            for card in log.cards_played(log.opponent):
                entry["cards"][card] = entry["cards"].get(card, 0) + 1
    views = []
    for entry in by_deck.values():
        entry["note"] = notes.get(entry["name"], "")
        entry["cards"] = sorted(entry["cards"].items(), key=lambda item: (-item[1], item[0]))
        views.append(entry)
    views.sort(key=lambda e: (-e["record"].total, e["name"].lower()))
    # What has been going around lately: the decks met in the last four weeks.
    since = (datetime.now(timezone.utc) - timedelta(weeks=4)).isoformat()
    recent: dict[str, insights.Record] = {}
    for match in matches:
        if match.opponent_deck and match.played_at_utc >= since:
            recent.setdefault(match.opponent_deck, insights.Record()).add(match.result)
    meta = sorted(recent.items(), key=lambda item: (-item[1].total, item[0].lower()))
    return templates.TemplateResponse(
        request,
        "opponents.html",
        {"opponents": views, "meta": meta, "meta_games": sum(r.total for _, r in meta)},
    )


@app.get("/weeks")
def weeks(request: Request):
    """What was played each week, with the lessons and misplays noted."""
    matches = storage.list_matches(db_path=db_path())
    by_match: dict[int, list[storage.Event]] = {}
    for event in storage.list_all_events(db_path=db_path()):
        by_match.setdefault(event.match_id, []).append(event)
    grouped: dict[tuple[int, int], dict] = {}
    for match in matches:  # newest first, so weeks come out newest first too
        played = datetime.fromisoformat(match.played_at_utc).astimezone()
        year, week, _ = played.isocalendar()
        monday = (played - timedelta(days=played.weekday())).date()
        entry = grouped.setdefault(
            (year, week),
            {
                "monday": monday,
                "sunday": monday + timedelta(days=6),
                "record": insights.Record(),
                "decks": {},
                "lessons": [],
                "misplays": [],
                "labels": {key: 0 for key in storage.LABELS},
            },
        )
        entry["record"].add(match.result)
        entry["decks"].setdefault(match.deck, insights.Record()).add(match.result)
        if match.lesson:
            entry["lessons"].append((match, match.lesson))
        for event in by_match.get(match.id, []):
            if event.label in entry["labels"]:
                entry["labels"][event.label] += 1
            if event.label == "misplay":
                entry["misplays"].append((match, event))
    # the sittings of each week: runs of matches logged within an hour of each other
    for sitting in insights.sessions(matches):
        first = datetime.fromisoformat(sitting[0].played_at_utc).astimezone()
        last = datetime.fromisoformat(sitting[-1].played_at_utc).astimezone()
        year, week, _ = first.isocalendar()
        record = insights.Record()
        for match in sitting:
            record.add(match.result)
        grouped[(year, week)].setdefault("sessions", []).append(
            {"day": first, "until": last, "record": record, "matches": sitting}
        )
    return templates.TemplateResponse(
        request, "weeks.html", {"weeks": list(grouped.values()), "labels": storage.LABELS}
    )


def _clock(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


@app.get("/matches/{match_id}/export")
def export_match(match_id: int):
    """A match as a text file: result, lesson, the battle log turn by turn,
    and every note and comment where it belongs."""
    match = _require_match(match_id)
    events = storage.list_events(match_id, db_path=db_path())
    log = battlelog.parse(match.battle_log) if match.battle_log else None
    played = datetime.fromisoformat(match.played_at_utc).astimezone()
    lines = [
        f"# {match.deck} vs {match.opponent_deck or '?'} — {match.result}",
        "",
        f"Played {played.strftime('%Y-%m-%d %H:%M')}"
        + (f", went {match.turn_order}" if match.turn_order else ""),
    ]
    if match.lesson:
        lines += ["", f"**Lesson:** {match.lesson}"]
    note = storage.matchup_notes(db_path=db_path()).get(match.opponent_deck or "")
    if note:
        lines += ["", f"**Against {match.opponent_deck}:** {note}"]
    if match.notes:
        lines += ["", match.notes]

    def written(event: storage.Event) -> str:
        label = f"[{storage.LABELS[event.label]}] " if event.label else ""
        text = event.detail or ("(spoken note)" if event.audio_file else "")
        return f"{label}{text}".strip()

    if log is not None and log.turns:
        hand = log.opening_hand()
        if hand:
            lines += ["", f"**Opening hand:** {', '.join(hand)}"]
        race = log.prize_race()
        lines += ["", f"**Prize cards taken:** you {race[-1]['you']}, opponent {race[-1]['opponent']}"]
        comments: dict[tuple[int, int], list[storage.Event]] = {}
        for event in events:
            if event.log_turn is not None:
                comments.setdefault((event.log_turn, event.log_action or 0), []).append(event)
        marks = sorted(e.video_offset_seconds for e in events if e.kind == "turn")
        timed = sorted(
            (e for e in events if e.kind in ("note", "mark") and e.log_turn is None and e.video_offset_seconds is not None),
            key=lambda e: e.video_offset_seconds,
        )
        aligned = len(marks) == len(log.turns)
        for number, turn in enumerate(log.turns, start=1):
            owner = "you" if turn.player == log.me else "opponent"
            at = f" ({_clock(marks[number - 1])})" if aligned else ""
            lines += ["", f"## Turn {number} — {owner}{at}", ""]
            for index, action in enumerate(log.narrated(turn)):
                lines.append(f"{index + 1}. {action.text}")
                lines += [f"   - {detail}" for detail in action.details]
                lines += [f"   > {written(c)}" for c in comments.get((number, index), [])]
            if aligned:
                end = marks[number] if number < len(marks) else float("inf")
                for event in timed:
                    if marks[number - 1] <= event.video_offset_seconds < end:
                        lines.append(f"- Note at {_clock(event.video_offset_seconds)}: {written(event) or 'flagged while recording'}")
        if not aligned and timed:
            lines += ["", "## Notes on the video", ""]
            lines += [f"- {_clock(e.video_offset_seconds)}: {written(e) or 'flagged while recording'}" for e in timed]
    else:
        notes = [e for e in events if e.kind in ("note", "mark")]
        if notes:
            lines += ["", "## Notes", ""]
            for event in notes:
                at = _clock(event.video_offset_seconds) if event.video_offset_seconds is not None else "match"
                lines.append(f"- {at}: {written(event) or 'flagged while recording'}")
    whole = [e for e in events if e.kind == "note" and e.video_offset_seconds is None and e.log_turn is None]
    if whole and log is not None and log.turns:
        lines += ["", "## Notes on the match", ""] + [f"- {written(e)}" for e in whole]
    name = f"{played.strftime('%Y-%m-%d')} {match.deck} vs {match.opponent_deck or 'unknown'}.md"
    safe = "".join(ch if ch.isalnum() or ch in " .-_" else "-" for ch in name)
    return Response(
        "\n".join(lines) + "\n",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


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
            # which words keep turning up in what was written down as a misplay
            "recurring": insights.recurring_words(
                [m.event.detail for m in insights.moments(matches, events, "misplay") if m.event.detail]
            )
            if label == "misplay"
            else [],
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
    # Straight to the match, opened on what to take from it.
    return RedirectResponse(
        f"/matches/{match_id}?wrapup=1" if video_path or log is not None else "/",
        status_code=303,
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
            "log_times": json.loads(match.log_times) if match.log_times else {},
            "matchup_note": storage.matchup_notes(db_path=db_path()).get(match.opponent_deck or "", ""),
            "known_decks": sorted(
                {
                    d
                    for m in storage.list_matches(db_path=db_path())
                    for d in (m.deck, m.opponent_deck)
                    if d
                }
            ),
            "not_in_list": _played_but_not_listed(match),
            "facts": _log_facts(match),
            "swing": _swings([match]).get(match.id),
            "wrap_up": request.query_params.get("wrapup") == "1",
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
        if other.opponent_auto:
            continue  # itself a guess from a log, not a name to pass on
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
            storage.set_opponent_deck(match_id, name, auto=True, db_path=db_path())


def _log_facts(match: storage.Match, key_cards: list[str] | None = None) -> insights.LogFacts | None:
    """What a match's battle log says about how the game went. `key_cards`
    are the Pokémon whose arrival is tracked; by default the final
    evolutions seen in this game."""
    if not match.battle_log:
        return None
    log = battlelog.parse(match.battle_log)
    if not log.turns or not log.me:
        return None
    activity = log.turn_activity(log.me)
    tracked = key_cards if key_cards is not None else log.final_forms(log.me)
    return insights.LogFacts(
        match=match,
        turns=len(log.turns),
        first_prize=log.first_prize(),
        setup={card: log.first_in_play(log.me, card) for card in tracked},
        my_turns=len(activity),
        turns_without_energy=sum(not turn["attached"] for turn in activity),
        turns_without_attack=sum(not turn["attacked"] for turn in activity),
    )


def _deck_log_facts(matches: list[storage.Match]) -> list[insights.LogFacts]:
    """Log facts for a set of matches, tracking per deck the same Pokémon
    in every game: any final evolution that deck got out in any of them.
    A game where one never arrived then counts as "never", not as absent."""
    key_cards: dict[str, list[str]] = {}
    for match in matches:
        if match.battle_log:
            log = battlelog.parse(match.battle_log)
            known = key_cards.setdefault(match.deck, [])
            known += [card for card in log.final_forms(log.me) if card not in known]
    facts = [_log_facts(match, key_cards.get(match.deck, [])) for match in matches]
    return [fact for fact in facts if fact is not None]


def _played_but_not_listed(match: storage.Match) -> list[str]:
    """Cards the battle log shows in my deck that the saved list lacks — a
    sign the list on the Decks page is out of date."""
    listed = _deck_cards(match)
    if not match.battle_log or not listed:
        return []
    log = battlelog.parse(match.battle_log)
    return sorted(log.cards_seen(log.me) - set(listed))


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
        "log_turn": event.log_turn,
        "log_action": event.log_action,
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
    locate_plays_in_background(match_id)
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
    _remove_recording(name)
    storage.clear_video(match_id, db_path=db_path())
    return RedirectResponse(f"/matches/{match_id}", status_code=303)


class MatchIn(BaseModel):
    deck: str
    opponent_deck: str = ""
    result: storage.Result


@app.put("/matches/{match_id}")
def edit_match(match_id: int, body: MatchIn):
    """Correct the deck, opponent or result a match was logged with."""
    _require_match(match_id)
    deck = body.deck.strip()
    if not deck:
        raise HTTPException(status_code=400, detail="A match needs a deck")
    storage.update_match(
        match_id, deck, body.opponent_deck.strip() or None, body.result, db_path=db_path()
    )
    return {"ok": True}


@app.post("/matches/{match_id}/delete")
def delete_match(match_id: int):
    """Remove a match with its notes, spoken notes and recording."""
    match = _require_match(match_id)
    active = active_recording()
    if active is not None and match.video_file and active.video_path.name == Path(match.video_file).name:
        raise HTTPException(status_code=409, detail="Still recording this video")
    for event in storage.list_events(match_id, db_path=db_path()):
        if event.audio_file:
            (RECORDINGS_DIR / VOICE_DIRNAME / Path(event.audio_file).name).unlink(missing_ok=True)
    if match.video_file:
        _remove_recording(Path(match.video_file).name)
    storage.delete_match(match_id, db_path=db_path())
    return RedirectResponse("/", status_code=303)


class MatchupNoteIn(BaseModel):
    opponent_deck: str
    note: str = ""


@app.put("/matchups/note")
def set_matchup_note(body: MatchupNoteIn):
    opponent = body.opponent_deck.strip()
    if not opponent:
        raise HTTPException(status_code=400, detail="Which opponent deck?")
    storage.set_matchup_note(opponent, body.note.strip(), db_path=db_path())
    return {"opponent_deck": opponent, "note": body.note.strip() or None}


@app.post("/settings/recording-retention")
def set_recording_retention(weeks: str = Form("")):
    """How long recordings are kept; empty for keeping them until deleted
    by hand."""
    if weeks and not (weeks.isdigit() and 1 <= int(weeks) <= 520):
        raise HTTPException(status_code=400, detail="Weeks must be a number")
    storage.set_setting("recording_retention_weeks", weeks or None, db_path=db_path())
    return RedirectResponse("/", status_code=303)


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
    # for a comment on a battle log line: its turn (0 = setup) and action
    log_turn: int | None = None
    log_action: int | None = None


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
        log_turn=note.log_turn,
        log_action=note.log_action,
        db_path=db_path(),
    )
    return _event_json(_require_event(event_id))


@app.post("/matches/{match_id}/voice")
async def add_voice_note(
    match_id: int,
    audio: UploadFile = File(...),
    offset_seconds: float | None = Form(None),
    log_turn: int | None = Form(None),
    log_action: int | None = Form(None),
    label: storage.Label | None = Form(None),
):
    """Save a spoken note recorded in the browser: at a moment in the
    video, or as a comment on a line of the battle log."""
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
        label=label,
        audio_file=filename,
        log_turn=log_turn,
        log_action=log_action,
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


def _store_log_times(match_id: int) -> None:
    """Find where the plays of the battle log happen in the recording.
    Needs the log's turns to line up one to one with the turn markers, and
    the deck's saved list for the card pictures. Only my own plays are
    looked for: the opponent's cards are known by name alone, and trying
    several printings of each found a third of them while taking most of
    the time (minutes per match)."""
    with _video_lock:
        match = storage.get_match(match_id, db_path=db_path())
        if match is None or not match.video_file or not match.battle_log:
            return
        log = battlelog.parse(match.battle_log)
        marks = sorted(
            e.video_offset_seconds
            for e in storage.list_events(match_id, db_path=db_path())
            if e.kind == "turn" and e.video_offset_seconds is not None
        )
        if not marks or len(marks) != len(log.turns):
            storage.set_log_times(match_id, None, db_path=db_path())
            return
        video_path = RECORDINGS_DIR / Path(match.video_file).name
        ends = marks[1:] + [recorder.video_duration(video_path)]
        listed = _deck_cards(match)
        plays: list[list[logtimes.Play]] = []
        for turn in log.turns:
            turn_plays = []
            if turn.player == log.me:
                for action, card in log.plays(turn):
                    printing = listed.get(card)
                    picture = (
                        cards.image_path(printing.set_code, printing.number)
                        if printing is not None and printing.printing
                        else None
                    )
                    if picture is not None:
                        turn_plays.append((action, picture))
            plays.append(turn_plays)
        found = logtimes.find_play_times(video_path, list(zip(marks, ends)), plays)
        storage.set_log_times(
            match_id,
            {f"{turn}-{action}": seconds for (turn, action), seconds in found.items()},
            db_path=db_path(),
        )


def _in_background(match_id: int, *jobs) -> None:
    """Run jobs for a match one after another off the request, counted in
    `_detecting` so the page knows to wait for them."""

    def work() -> None:
        try:
            for job in jobs:
                job()
        except Exception:
            # The buttons on the page still work; leave a trace in the log.
            traceback.print_exc()
        finally:
            with _detecting_lock:
                _detecting[match_id] -= 1
                if _detecting[match_id] <= 0:
                    del _detecting[match_id]

    with _detecting_lock:
        _detecting[match_id] = _detecting.get(match_id, 0) + 1
    threading.Thread(target=work, daemon=True).start()


def locate_plays_in_background(match_id: int) -> None:
    _in_background(match_id, lambda: _store_log_times(match_id))


def detect_turns_in_background(match_id: int) -> None:
    """Start looking for turns right away, so they are there (or nearly)
    by the time the review page is open; then for the plays of the log."""
    _in_background(
        match_id,
        lambda: _store_detected_turns(match_id, replace=False),
        lambda: _store_log_times(match_id),
    )


def _timeline_json(match_id: int) -> dict:
    # Whether a search is still running is asked first: if it isn't, what
    # is read after this is the finished result, not a half-written one.
    detecting = match_id in _detecting
    match = _require_match(match_id)
    return {
        "events": [_event_json(e) for e in storage.list_events(match_id, db_path=db_path())],
        "turn_order": match.turn_order,
        "detecting": detecting,
        "log_times": json.loads(match.log_times) if match.log_times else {},
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
    locate_plays_in_background(match_id)
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
