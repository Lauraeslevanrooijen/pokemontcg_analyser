from pathlib import Path

from pokemontcg_analyser import decks, storage

V1 = """Pokémon: 3
4 Dragapult ex TWM 130
2 Fezandipiti ex SFA 38

Trainer: 2
4 Ultra Ball SVI 196
3 Iono PAL 185
"""

V2 = """Pokémon: 3
4 Dragapult ex TWM 130
1 Fezandipiti ex SFA 38

Trainer: 3
4 Ultra Ball SVI 196
4 Iono PAL 185
2 Counter Catcher PAR 160
"""


def test_parse_skips_section_headers() -> None:
    assert decks.parse(V1) == {
        "Dragapult ex TWM 130": 4,
        "Fezandipiti ex SFA 38": 2,
        "Ultra Ball SVI 196": 4,
        "Iono PAL 185": 3,
    }
    assert decks.card_count(V1) == 13


def test_diff_lists_additions_then_removals() -> None:
    assert decks.diff(V1, V2) == [
        (2, "Counter Catcher PAR 160"),
        (1, "Iono PAL 185"),
        (-1, "Fezandipiti ex SFA 38"),
    ]
    assert decks.diff(V1, V1) == []


def test_matches_count_towards_the_version_current_when_logged(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    before = storage.log_match(deck="Pult", result="loss", db_path=db)
    v1 = storage.add_deck_version("Pult", V1, db_path=db)
    with_v1 = storage.log_match(deck="Pult", result="win", db_path=db)
    v2 = storage.add_deck_version("Pult", V2, "more disruption", db_path=db)
    with_v2 = storage.log_match(deck="Pult", result="win", db_path=db)
    other = storage.log_match(deck="Slob", result="win", db_path=db)

    versions = {m.id: m.deck_version_id for m in storage.list_matches(db_path=db)}

    assert versions == {before: None, with_v1: v1, with_v2: v2, other: None}


def test_card_names_drops_set_code_and_number() -> None:
    assert decks.card_names(V1) == ["Dragapult ex", "Fezandipiti ex", "Ultra Ball", "Iono"]
