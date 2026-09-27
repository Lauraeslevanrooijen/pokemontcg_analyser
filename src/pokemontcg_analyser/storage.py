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

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    played_at_utc TEXT NOT NULL,
    deck TEXT NOT NULL,
    opponent_deck TEXT,
    result TEXT NOT NULL CHECK (result IN ('win', 'loss', 'tie')),
    notes TEXT,
    video_file TEXT
);

CREATE TABLE IF NOT EXISTS match_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    video_offset_seconds REAL,
    kind TEXT NOT NULL,
    detail TEXT
);
"""


@contextmanager
def connect(db_path: Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
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


def log_match(
    deck: str,
    result: Result,
    opponent_deck: str | None = None,
    notes: str | None = None,
    video_file: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO matches (played_at_utc, deck, opponent_deck, result, notes, video_file)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                deck,
                opponent_deck,
                result,
                notes,
                video_file,
            ),
        )
        return cur.lastrowid


def list_matches(db_path: Path = DEFAULT_DB_PATH) -> list[Match]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM matches ORDER BY played_at_utc DESC"
        ).fetchall()
        return [Match(**dict(row)) for row in rows]


def add_events(
    match_id: int,
    events: list[tuple[float | None, str, str | None]],
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO match_events (match_id, video_offset_seconds, kind, detail)
            VALUES (?, ?, ?, ?)
            """,
            [(match_id, offset, kind, detail) for offset, kind, detail in events],
        )


def add_note(
    match_id: int,
    text: str,
    offset_seconds: float | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Attach a timestamped note to a match. Omit offset for a note about
    the match as a whole (e.g. a post-game reflection)."""
    add_events(match_id, [(offset_seconds, "note", text)], db_path=db_path)


@dataclass(frozen=True)
class Event:
    id: int
    match_id: int
    video_offset_seconds: float | None
    kind: str
    detail: str | None


def list_events(match_id: int, db_path: Path = DEFAULT_DB_PATH) -> list[Event]:
    """A match's events (scene changes, notes, ...) in chronological order.
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


def compute_stats(db_path: Path = DEFAULT_DB_PATH) -> Stats:
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
