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
    assert stats.label_counts == {"misplay": 3, "good": 1, "key": 0, "luck": 0}
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
