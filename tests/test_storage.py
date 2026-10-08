from pathlib import Path

from pokemontcg_analyser import storage


def test_log_and_list_matches(tmp_path: Path) -> None:
    db_path = tmp_path / "matches.db"

    id1 = storage.log_match(deck="Charizard ex", result="win", db_path=db_path)
    id2 = storage.log_match(
        deck="Lost Box",
        result="loss",
        opponent_deck="Charizard ex",
        notes="close game",
        db_path=db_path,
    )

    matches = storage.list_matches(db_path=db_path)
    assert [m.id for m in matches] == [id2, id1]  # most recent first
    assert matches[0].deck == "Lost Box"
    assert matches[0].opponent_deck == "Charizard ex"
    assert matches[0].result == "loss"
    assert matches[0].notes == "close game"


def test_compute_stats(tmp_path: Path) -> None:
    db_path = tmp_path / "matches.db"
    storage.log_match(deck="Charizard ex", result="win", db_path=db_path)
    storage.log_match(deck="Charizard ex", result="win", db_path=db_path)
    storage.log_match(deck="Charizard ex", result="loss", db_path=db_path)
    storage.log_match(deck="Lost Box", result="win", db_path=db_path)

    stats = storage.compute_stats(db_path=db_path)
    assert stats.total == 4
    assert stats.wins == 3
    assert stats.losses == 1
    assert stats.ties == 0
    assert stats.win_rate == 0.75
    assert stats.by_deck["Charizard ex"] == (2, 1, 0)
    assert stats.by_deck["Lost Box"] == (1, 0, 0)


def test_events_and_notes_ordered(tmp_path: Path) -> None:
    db_path = tmp_path / "matches.db"
    match_id = storage.log_match(deck="Charizard ex", result="win", db_path=db_path)

    storage.add_events(
        match_id,
        [(10.0, "scene_change", "score=20.0"), (5.0, "scene_change", "score=15.0")],
        db_path=db_path,
    )
    storage.add_note(match_id, "great opening hand", offset_seconds=1.0, db_path=db_path)
    storage.add_note(match_id, "should not have conceded", db_path=db_path)  # no offset

    events = storage.list_events(match_id, db_path=db_path)

    assert [e.detail for e in events] == [
        "should not have conceded",  # no offset sorts first
        "great opening hand",
        "score=15.0",
        "score=20.0",
    ]
    assert events[0].video_offset_seconds is None
    assert events[1].kind == "note"
    assert events[2].video_offset_seconds == 5.0


def test_add_note_isolated_to_match(tmp_path: Path) -> None:
    db_path = tmp_path / "matches.db"
    match1 = storage.log_match(deck="A", result="win", db_path=db_path)
    match2 = storage.log_match(deck="B", result="loss", db_path=db_path)

    storage.add_note(match1, "note for match 1", db_path=db_path)

    assert len(storage.list_events(match1, db_path=db_path)) == 1
    assert len(storage.list_events(match2, db_path=db_path)) == 0


def test_label_column_is_added_to_an_older_database(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE matches (id INTEGER PRIMARY KEY AUTOINCREMENT, played_at_utc TEXT NOT NULL,
            deck TEXT NOT NULL, opponent_deck TEXT, result TEXT NOT NULL, notes TEXT, video_file TEXT);
        CREATE TABLE match_events (id INTEGER PRIMARY KEY AUTOINCREMENT, match_id INTEGER NOT NULL,
            video_offset_seconds REAL, kind TEXT NOT NULL, detail TEXT);
        INSERT INTO matches VALUES (1, '2026-01-01T00:00:00+00:00', 'Slob', NULL, 'win', NULL, NULL);
        INSERT INTO match_events VALUES (1, 1, 5.0, 'note', 'old note');
        """
    )
    conn.commit()
    conn.close()

    storage.add_note(1, "new note", offset_seconds=9.0, label="key", db_path=db)

    events = storage.list_events(1, db_path=db)
    assert [(e.detail, e.label) for e in events] == [("old note", None), ("new note", "key")]

    storage.set_turn_order(1, "first", db_path=db)
    assert storage.get_match(1, db_path=db).turn_order == "first"
