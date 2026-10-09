from pathlib import Path

from pokemontcg_analyser import insights, storage


def _seed(db: Path) -> tuple[int, int, int]:
    won = storage.log_match(deck="Pult", opponent_deck="Iono", result="win", db_path=db)
    lost = storage.log_match(deck="Pult", opponent_deck="Zard", result="loss", db_path=db)
    unreviewed = storage.log_match(deck="Slob", result="loss", db_path=db)
    storage.set_turn_order(won, "second", db_path=db)
    storage.set_turn_order(lost, "first", db_path=db)

    storage.add_event(won, "turn", offset_seconds=10.0, db_path=db)
    storage.add_event(won, "turn", offset_seconds=60.0, db_path=db)
    storage.add_note(won, "kept a bad hand", offset_seconds=5.0, label="misplay", db_path=db)
    storage.add_note(won, "wrong attacker", offset_seconds=70.0, label="misplay", db_path=db)
    storage.add_note(won, "nice gust", offset_seconds=80.0, label="good", db_path=db)
    # no turn markers in this one
    storage.add_note(lost, "benched too much", offset_seconds=30.0, label="misplay", db_path=db)
    return won, lost, unreviewed


def test_build_counts_records_and_misplays(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    _seed(db)

    stats = insights.build(storage.list_matches(db_path=db), storage.list_all_events(db_path=db))

    assert (stats.overall.wins, stats.overall.losses, stats.overall.total) == (1, 2, 3)
    assert stats.by_deck["Pult"].win_rate == 0.5
    assert stats.by_matchup[("Slob", "?")].losses == 1
    assert stats.by_turn_order["second"].wins == 1
    assert stats.by_turn_order["unknown"].total == 1
    # the Slob game has no notes, so it does not dilute the averages
    assert stats.reviewed_matches == 2
    assert stats.label_counts == {
        "misplay": 3, "good": 1, "key": 0, "luck": 0, "topdeck": 0, "note": 0,
    }
    assert stats.misplays_by_result == {"win": (2, 1), "loss": (1, 1)}
    # 70s is in turn 2; 5s is before turn 1; the other game has no turns
    assert stats.misplays_by_turn == {2: 1}


def test_moments_filters_by_label_and_finds_the_turn(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    won, lost, _ = _seed(db)
    matches, events = storage.list_matches(db_path=db), storage.list_all_events(db_path=db)

    misplays = insights.moments(matches, events, "misplay")

    assert [(m.match.id, m.event.detail, m.turn) for m in misplays] == [
        (lost, "benched too much", None),
        (won, "kept a bad hand", 0),
        (won, "wrong attacker", 2),
    ]
    assert len(insights.moments(matches, events)) == 4


def test_openings_counts_cards_and_hand_features() -> None:
    kinds = {"Dreepy": "Basic Pokémon", "Budew": "Basic Pokémon", "Iono": "Supporter", "Fire": "Energy"}
    hands = [
        ("win", ["Dreepy", "Dreepy", "Iono", "Fire", "Fire"], kinds),
        ("loss", ["Budew", "Fire"], kinds),
        ("loss", ["Dreepy", "Mystery card"], kinds),  # a card the list doesn't have
        ("win", ["Dreepy"], {}),  # no saved list at all
    ]

    found = insights.openings(hands)

    assert found.games == 4
    dreepy = found.by_card["Dreepy"]
    assert (dreepy.total, dreepy.wins, dreepy.losses) == (3, 2, 1)  # once per hand, not per copy
    assert list(found.by_card)[0] == "Dreepy"  # most frequent first
    # only the two hands whose every card is known count for the features
    assert found.typed_games == 2
    with_supporter, without = found.by_feature["A Supporter in hand"]
    assert (with_supporter.wins, without.losses) == (1, 1)
    one_basic, _ = found.by_feature["Only one Basic Pokémon"]
    assert (one_basic.total, one_basic.losses) == (1, 1)
    assert found.average == {"Basic Pokémon": 1.5, "Energy": 1.5, "Supporter": 0.5}


def test_a_comment_on_a_log_line_counts_in_its_turn(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    match_id = storage.log_match(deck="Pult", result="loss", db_path=db)
    storage.add_note(match_id, "wrong target", label="misplay", log_turn=6, log_action=2, db_path=db)
    matches, events = storage.list_matches(db_path=db), storage.list_all_events(db_path=db)

    assert insights.build(matches, events).misplays_by_turn == {6: 1}
    assert insights.moments(matches, events, "misplay")[0].turn == 6


def test_turn_lengths_and_tempo(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    detected = storage.log_match(deck="Pult", result="win", db_path=db)
    storage.replace_turns(detected, [(10.0, "opponent"), (40.0, "you"), (130.0, "opponent")], db_path=db)
    by_hand = storage.log_match(deck="Pult", result="loss", db_path=db)
    storage.set_turn_order(by_hand, "first", db_path=db)
    for offset in (0.0, 60.0, 80.0):
        storage.add_event(by_hand, "turn", offset_seconds=offset, db_path=db)
    matches, events = storage.list_matches(db_path=db), storage.list_all_events(db_path=db)
    first = next(m for m in matches if m.id == detected)

    lengths = insights.turn_lengths(first, [e for e in events if e.match_id == detected])
    assert lengths == [(1, "opponent", 30.0), (2, "you", 90.0)]  # the last turn has no end

    # by hand: I went first, so turn 1 (60s) is mine and turn 2 (20s) the opponent's
    assert insights.tempo(matches, events) == {"you": (75.0, 2), "opponent": (25.0, 2)}


def test_game_lengths_overall_by_result_and_by_deck(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    storage.log_match(deck="Pult", result="win", db_path=db)
    storage.log_match(deck="Pult", result="loss", db_path=db)
    storage.log_match(deck="Slob", result="loss", db_path=db)
    pult_win, pult_loss, slob_loss = sorted(storage.list_matches(db_path=db), key=lambda m: m.id)

    found = insights.game_lengths([(pult_win, 7), (pult_loss, 16), (slob_loss, 5)])

    assert list(found) == ["All games", "Wins", "Losses", "With Pult", "With Slob"]
    everything = found["All games"]
    assert (everything.games, round(everything.average, 2), everything.shortest, everything.longest) == (3, 9.33, 5, 16)
    assert found["Losses"].average == 10.5
    assert found["With Pult"].average == 11.5
    assert insights.game_lengths([]) == {}


def _match(result: str, played: str = "2026-01-01T12:00:00+00:00", match_id: int = 1) -> storage.Match:
    return storage.Match(match_id, played, "Pult", None, result, None, None)


def test_prize_swing_spots_comebacks_and_lost_leads() -> None:
    behind_then_won = [{"you": 0, "opponent": 3}, {"you": 6, "opponent": 3}]
    ahead_then_lost = [{"you": 2, "opponent": 0}, {"you": 2, "opponent": 6}]

    assert insights.prize_swing(_match("win"), behind_then_won) == ("comeback", 3)
    assert insights.prize_swing(_match("loss"), ahead_then_lost) == ("lead lost", 2)
    # one Prize card either way is just a game
    assert insights.prize_swing(_match("win"), [{"you": 0, "opponent": 1}, {"you": 6, "opponent": 1}]) is None
    # being behind only makes a comeback if the game was then won
    assert insights.prize_swing(_match("loss"), [{"you": 0, "opponent": 3}, {"you": 1, "opponent": 6}]) is None
    assert insights.prize_swing(_match("win"), []) is None


def test_longest_streak_and_the_one_still_going() -> None:
    results = ["win", "win", "win", "loss", "win", "win"]
    matches = [_match(r, f"2026-01-0{i + 1}T12:00:00+00:00", i) for i, r in enumerate(results)]

    assert insights.longest_streak(matches) == (3, 2)
    assert insights.longest_streak(matches[:4]) == (3, 0)
    assert insights.longest_streak([]) == (0, 0)


def test_sessions_group_matches_logged_close_together() -> None:
    times = ["2026-01-01T19:00", "2026-01-01T19:25", "2026-01-01T20:10", "2026-01-01T22:30", "2026-01-02T19:00"]
    matches = [_match("win", f"{t}:00+00:00", i) for i, t in enumerate(times)]

    grouped = insights.sessions(list(reversed(matches)))

    # newest sitting first; 20:10 is within the hour of 19:25, 22:30 is not
    assert [[m.id for m in sitting] for sitting in grouped] == [[4], [3], [0, 1, 2]]


def test_recurring_words_across_notes() -> None:
    notes = [
        "Had moeten retreaten met Budew",
        "Vergeten te retreaten, Dragapult stond klaar",
        "Te veel op de bank gezet",
        "Bank vol voordat ik Dragapult had",
    ]

    assert insights.recurring_words(notes) == [("bank", 2), ("dragapult", 2), ("retreaten", 2)]
    assert insights.recurring_words(["only once here"]) == []
