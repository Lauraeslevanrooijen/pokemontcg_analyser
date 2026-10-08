"""SQLite storage for matches and analysis events."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

DEFAULT_DB_PATH = Path("data/matches.db")

Result = Literal["win", "loss", "tie"]

TurnOrder = Literal["first", "second"]

Label = Literal["misplay", "good", "key", "luck"]
LABELS: dict[str, str] = {
    "misplay": "Misplay",
    "good": "Good play",
    "key": "Key moment",
    "luck": "Bad luck",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    played_at_utc TEXT NOT NULL,
    deck TEXT NOT NULL,
    opponent_deck TEXT,
    result TEXT NOT NULL CHECK (result IN ('win', 'loss', 'tie')),
    notes TEXT,
    video_file TEXT,
    turn_order TEXT CHECK (turn_order IN ('first', 'second')),
    game_start_seconds REAL,
    lesson TEXT,
    deck_version_id INTEGER REFERENCES deck_versions(id),
    battle_log TEXT,
    opponent_auto INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS matchup_notes (
    opponent_deck TEXT PRIMARY KEY,
    note TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deck_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deck TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    decklist TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS match_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    video_offset_seconds REAL,
    kind TEXT NOT NULL,
    detail TEXT,
    label TEXT,
    audio_file TEXT,
    log_turn INTEGER,
    log_action INTEGER
);
"""


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    db_path = db_path if db_path is not None else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        # Databases created before these columns existed lack them.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(match_events)")}
        if "label" not in columns:
            conn.execute("ALTER TABLE match_events ADD COLUMN label TEXT")
        if "audio_file" not in columns:
            conn.execute("ALTER TABLE match_events ADD COLUMN audio_file TEXT")
        if "log_turn" not in columns:
            conn.execute("ALTER TABLE match_events ADD COLUMN log_turn INTEGER")
            conn.execute("ALTER TABLE match_events ADD COLUMN log_action INTEGER")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(matches)")}
        if "turn_order" not in columns:
            conn.execute("ALTER TABLE matches ADD COLUMN turn_order TEXT")
        if "game_start_seconds" not in columns:
            conn.execute("ALTER TABLE matches ADD COLUMN game_start_seconds REAL")
        if "lesson" not in columns:
            conn.execute("ALTER TABLE matches ADD COLUMN lesson TEXT")
        if "deck_version_id" not in columns:
            conn.execute("ALTER TABLE matches ADD COLUMN deck_version_id INTEGER")
        if "battle_log" not in columns:
            conn.execute("ALTER TABLE matches ADD COLUMN battle_log TEXT")
        if "opponent_auto" not in columns:
            conn.execute("ALTER TABLE matches ADD COLUMN opponent_auto INTEGER NOT NULL DEFAULT 0")
        yield conn
        conn.commit()
    finally:
        conn.close()


@dataclass(frozen=True)
class Match:
    id: int
    played_at_utc: str
    deck: str
    opponent_deck: str | None
    result: Result
    notes: str | None
    video_file: str | None
    turn_order: TurnOrder | None = None  # whether you went first or second
    # Where in the recording the game itself begins (after menus/matchmaking).
    game_start_seconds: float | None = None
    lesson: str | None = None  # the one thing to take away from this game
    # The saved list of this deck that was current when the match was logged.
    deck_version_id: int | None = None
    battle_log: str | None = None  # as copied from the game after the match
    # 1 when the opponent's deck was named from the battle log rather than
    # typed in: such a name is a guess, and is not copied to later matches.
    opponent_auto: int = 0


def log_match(
    deck: str,
    result: Result,
    opponent_deck: str | None = None,
    notes: str | None = None,
    video_file: str | None = None,
    db_path: Path | None = None,
) -> int:
    with connect(db_path) as conn:
        version = conn.execute(
            "SELECT id FROM deck_versions WHERE deck = ? ORDER BY id DESC LIMIT 1", (deck,)
        ).fetchone()
        cur = conn.execute(
            """
            INSERT INTO matches
                (played_at_utc, deck, opponent_deck, result, notes, video_file, deck_version_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                deck,
                opponent_deck,
                result,
                notes,
                video_file,
                version["id"] if version else None,
            ),
        )
        return cur.lastrowid


@dataclass(frozen=True)
class DeckVersion:
    id: int
    deck: str
    created_at_utc: str
    decklist: str
    note: str | None


def add_deck_version(
    deck: str, decklist: str, note: str | None = None, db_path: Path | None = None
) -> int:
    """Save a new version of a deck's list; matches logged from now on
    count towards it."""
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO deck_versions (deck, created_at_utc, decklist, note)
            VALUES (?, ?, ?, ?)
            """,
            (deck, datetime.now(timezone.utc).isoformat(), decklist, note or None),
        )
        return cur.lastrowid


def list_deck_versions(db_path: Path | None = None) -> list[DeckVersion]:
    """All saved lists, oldest first."""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM deck_versions ORDER BY id").fetchall()
        return [DeckVersion(**dict(row)) for row in rows]


def get_match(match_id: int, db_path: Path | None = None) -> Match | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM matches WHERE id = ?", (match_id,)
        ).fetchone()
        return Match(**dict(row)) if row is not None else None


def set_turn_order(
    match_id: int, turn_order: TurnOrder | None, db_path: Path | None = None
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE matches SET turn_order = ? WHERE id = ?", (turn_order, match_id)
        )


def set_battle_log(match_id: int, text: str | None, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE matches SET battle_log = ? WHERE id = ?", (text or None, match_id))


def set_opponent_deck(
    match_id: int, name: str | None, auto: bool = False, db_path: Path | None = None
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE matches SET opponent_deck = ?, opponent_auto = ? WHERE id = ?",
            (name or None, int(auto), match_id),
        )


def update_match(
    match_id: int,
    deck: str,
    opponent_deck: str | None,
    result: Result,
    db_path: Path | None = None,
) -> None:
    """Correct what a match was logged as. A different deck name moves the
    match to that deck's newest saved list."""
    with connect(db_path) as conn:
        current = conn.execute(
            "SELECT deck, opponent_deck FROM matches WHERE id = ?", (match_id,)
        ).fetchone()
        if current is None:
            return
        conn.execute(
            "UPDATE matches SET deck = ?, opponent_deck = ?, result = ? WHERE id = ?",
            (deck, opponent_deck or None, result, match_id),
        )
        if (opponent_deck or None) != current["opponent_deck"]:
            conn.execute("UPDATE matches SET opponent_auto = 0 WHERE id = ?", (match_id,))
        if deck != current["deck"]:
            version = conn.execute(
                "SELECT id FROM deck_versions WHERE deck = ? ORDER BY id DESC LIMIT 1", (deck,)
            ).fetchone()
            conn.execute(
                "UPDATE matches SET deck_version_id = ? WHERE id = ?",
                (version["id"] if version else None, match_id),
            )


def delete_match(match_id: int, db_path: Path | None = None) -> None:
    """Remove a match and everything on its timeline."""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))
        conn.execute("DELETE FROM matches WHERE id = ?", (match_id,))


def get_setting(key: str, default: str | None = None, db_path: Path | None = None) -> str | None:
    with connect(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default


def set_setting(key: str, value: str | None, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def matchup_notes(db_path: Path | None = None) -> dict[str, str]:
    """What to remember against each opponent deck, by its name."""
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM matchup_notes ORDER BY opponent_deck").fetchall()
        return {row["opponent_deck"]: row["note"] for row in rows}


def set_matchup_note(opponent_deck: str, note: str, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        if note:
            conn.execute(
                "INSERT INTO matchup_notes (opponent_deck, note) VALUES (?, ?) "
                "ON CONFLICT(opponent_deck) DO UPDATE SET note = excluded.note",
                (opponent_deck, note),
            )
        else:
            conn.execute("DELETE FROM matchup_notes WHERE opponent_deck = ?", (opponent_deck,))


def backup_database(destination: Path, db_path: Path | None = None) -> None:
    """Write a consistent copy of the database, safe while it is in use."""
    source_path = db_path if db_path is not None else DEFAULT_DB_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def set_result(match_id: int, result: Result, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE matches SET result = ? WHERE id = ?", (result, match_id))


def set_lesson(match_id: int, lesson: str | None, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE matches SET lesson = ? WHERE id = ?", (lesson or None, match_id))


def clear_video(match_id: int, db_path: Path | None = None) -> None:
    """Detach a match from its recording (the notes stay) and forget the
    game start, which only meant something as a position in that video."""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE matches SET video_file = NULL, game_start_seconds = NULL WHERE id = ?",
            (match_id,),
        )


def set_game_start(
    match_id: int, offset_seconds: float | None, db_path: Path | None = None
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE matches SET game_start_seconds = ? WHERE id = ?",
            (offset_seconds, match_id),
        )


def shift_offsets(match_id: int, seconds: float, db_path: Path | None = None) -> None:
    """Move a match's game start and events `seconds` earlier, after that
    much was cut off the front of its recording. Anything that sat in the
    removed part ends up at the very start."""
    with connect(db_path) as conn:
        conn.execute(
            """
            UPDATE match_events SET video_offset_seconds = MAX(0, video_offset_seconds - ?)
            WHERE match_id = ? AND video_offset_seconds IS NOT NULL
            """,
            (seconds, match_id),
        )
        conn.execute(
            """
            UPDATE matches SET game_start_seconds = MAX(0, game_start_seconds - ?)
            WHERE id = ? AND game_start_seconds IS NOT NULL
            """,
            (seconds, match_id),
        )


def list_matches(db_path: Path | None = None) -> list[Match]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM matches ORDER BY played_at_utc DESC"
        ).fetchall()
        return [Match(**dict(row)) for row in rows]


def add_events(
    match_id: int,
    events: list[tuple[float | None, str, str | None]],
    db_path: Path | None = None,
) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO match_events (match_id, video_offset_seconds, kind, detail)
            VALUES (?, ?, ?, ?)
            """,
            [(match_id, offset, kind, detail) for offset, kind, detail in events],
        )


def add_event(
    match_id: int,
    kind: str,
    offset_seconds: float | None = None,
    detail: str | None = None,
    label: Label | None = None,
    audio_file: str | None = None,
    log_turn: int | None = None,
    log_action: int | None = None,
    db_path: Path | None = None,
) -> int:
    """Insert a single event and return its id."""
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO match_events
                (match_id, video_offset_seconds, kind, detail, label, audio_file,
                 log_turn, log_action)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (match_id, offset_seconds, kind, detail, label, audio_file, log_turn, log_action),
        )
        return cur.lastrowid


def add_note(
    match_id: int,
    text: str,
    offset_seconds: float | None = None,
    label: Label | None = None,
    log_turn: int | None = None,
    log_action: int | None = None,
    db_path: Path | None = None,
) -> int:
    """Attach a note to a match: at a moment in the video (`offset_seconds`),
    on a line of the battle log (`log_turn` and `log_action`), or with
    neither for a note about the match as a whole. Returns the new event's
    id."""
    return add_event(
        match_id,
        "note",
        offset_seconds,
        text or None,
        label,
        log_turn=log_turn,
        log_action=log_action,
        db_path=db_path,
    )


@dataclass(frozen=True)
class Event:
    id: int
    match_id: int
    video_offset_seconds: float | None
    kind: str
    detail: str | None
    label: str | None = None
    audio_file: str | None = None  # a spoken note: filename in recordings/voice/
    # A comment on a line of the battle log: which turn (0 is the setup)
    # and which action in it, both counted as the log has them.
    log_turn: int | None = None
    log_action: int | None = None


def list_events(match_id: int, db_path: Path | None = None) -> list[Event]:
    """A match's events (notes, turn markers, ...) in chronological order.
    Events without an offset (whole-match notes) sort first."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM match_events
            WHERE match_id = ?
            ORDER BY video_offset_seconds IS NOT NULL, video_offset_seconds
            """,
            (match_id,),
        ).fetchall()
        return [Event(**dict(row)) for row in rows]


def list_all_events(db_path: Path | None = None) -> list[Event]:
    """Every match's events, each match's in chronological order."""
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM match_events
            ORDER BY match_id, video_offset_seconds IS NOT NULL, video_offset_seconds
            """
        ).fetchall()
        return [Event(**dict(row)) for row in rows]


def get_event(event_id: int, db_path: Path | None = None) -> Event | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM match_events WHERE id = ?", (event_id,)
        ).fetchone()
        return Event(**dict(row)) if row is not None else None


def update_note(
    event_id: int,
    text: str,
    label: Label | None,
    db_path: Path | None = None,
) -> None:
    """Rewrite an event's text and label. A bare mark made during recording
    becomes a regular note once it has been filled in."""
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE match_events SET detail = ?, label = ?, kind = 'note' WHERE id = ?",
            (text or None, label, event_id),
        )


def set_event_detail(event_id: int, detail: str, db_path: Path | None = None) -> None:
    """Set an event's text, leaving its kind and label alone."""
    with connect(db_path) as conn:
        conn.execute("UPDATE match_events SET detail = ? WHERE id = ?", (detail, event_id))


def replace_turns(
    match_id: int,
    turns: list[tuple[float, str]],
    db_path: Path | None = None,
) -> None:
    """Swap a match's turn markers for detected ones. Each is an offset and
    whose turn it is ("you" or "opponent"), kept in the event's detail."""
    with connect(db_path) as conn:
        conn.execute(
            "DELETE FROM match_events WHERE match_id = ? AND kind = 'turn'", (match_id,)
        )
        conn.executemany(
            """
            INSERT INTO match_events (match_id, video_offset_seconds, kind, detail)
            VALUES (?, ?, 'turn', ?)
            """,
            [(match_id, offset, owner) for offset, owner in turns],
        )


def delete_event(event_id: int, db_path: Path | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM match_events WHERE id = ?", (event_id,))


@dataclass(frozen=True)
class Stats:
    total: int
    wins: int
    losses: int
    ties: int
    by_deck: dict[str, tuple[int, int, int]]  # deck -> (wins, losses, ties)

    @property
    def win_rate(self) -> float:
        return self.wins / self.total if self.total else 0.0


def compute_stats(db_path: Path | None = None) -> Stats:
    matches = list_matches(db_path)
    by_deck: dict[str, list[int]] = {}
    wins = losses = ties = 0
    for m in matches:
        counts = by_deck.setdefault(m.deck, [0, 0, 0])
        if m.result == "win":
            wins += 1
            counts[0] += 1
        elif m.result == "loss":
            losses += 1
            counts[1] += 1
        else:
            ties += 1
            counts[2] += 1
    return Stats(
        total=len(matches),
        wins=wins,
        losses=losses,
        ties=ties,
        by_deck={deck: tuple(c) for deck, c in by_deck.items()},
    )
