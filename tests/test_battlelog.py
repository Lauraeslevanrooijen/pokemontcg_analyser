from pathlib import Path

from pokemontcg_analyser import battlelog

LOG = (Path(__file__).parent / "data" / "battle_log.txt").read_text()


def test_parse_finds_players_order_and_winner() -> None:
    log = battlelog.parse(LOG)

    # the log spells out only my own opening hand, which is how "me" is told
    assert (log.me, log.opponent) == ("Misty", "Brock")
    assert log.first_player == "Brock"
    assert log.turn_order == "second"
    assert (log.winner, log.result) == ("Misty", "win")
    assert [turn.player for turn in log.turns] == ["Brock", "Misty", "Brock", "Misty"]


def test_details_and_card_lists_attach_to_their_action() -> None:
    log = battlelog.parse(LOG)

    ultra_ball = log.turns[0].actions[1]
    assert ultra_ball.text == "Brock played Ultra Ball."
    assert ultra_ball.details == [
        "Brock discarded 2 cards: Banette, Poltchageist",
        "Brock drew Dhelmise.",
        "Brock shuffled their deck.",
    ]
    assert len(log.setup) == 7
    assert log.setup[3].details[0].startswith("7 drawn cards: Buddy-Buddy Poffin, Budew")


def test_pokemon_the_opponent_showed() -> None:
    log = battlelog.parse(LOG)

    assert log.pokemon_of(log.opponent) == ["Poltchageist", "Dhelmise"]


def test_narrated_drops_the_turn_owner_and_names_the_sides() -> None:
    log = battlelog.parse(LOG)

    mine = [a.text for a in log.narrated(log.turns[1])]
    assert mine[1] == "Played Buddy-Buddy Poffin."
    # curly apostrophe in the game's text, and the opponent named by side
    assert mine[-1] == "Budew used Itchy Pollen on opponent's Poltchageist for 10 damage."
    assert log.narrated(log.turns[3])[-1].text == "Opponent conceded. You win."
    assert log.narrated(log.turns[0])[1].details[0] == "Discarded 2 cards: Banette, Poltchageist"


def test_numbered_turn_headers_and_going_first() -> None:
    log = battlelog.parse(
        "Setup\nAsh decided to go first.\nAsh drew 7 cards for the opening hand.\n- 7 drawn cards.\n"
        "   \u2022 Pikachu\n\nTurn # 1 - Ash's Turn\nAsh drew Iono.\n\n"
        "Turn # 2 - Gary's Turn\nGary drew a card.\nAsh conceded. Gary wins.\n"
    )

    assert (log.me, log.turn_order, log.result) == ("Ash", "first", "loss")
    assert len(log.turns) == 2


def test_text_that_is_not_a_log() -> None:
    log = battlelog.parse("just some notes\nabout a game")

    assert log.turns == [] and log.me is None and log.result is None


LONGER = """Setup
Ash decided to go first.
Ash drew 7 cards for the opening hand.
- 7 drawn cards.
   • Pikachu, Iono
Ash took a mulligan.
Ash drew 7 cards for the opening hand.
- 7 drawn cards.
   • Raichu, Iono, Basic Lightning Energy
Gary drew 7 cards for the opening hand.
- 7 drawn cards.

Ash's Turn
Ash's Raichu used Thunder on Gary's Tauros for 120 damage.
- Damage breakdown:
   • Base damage: 100 damage
   • Weakness: 20 damage
Gary's Tauros was Knocked Out!
Ash took 2 Prize cards.

Gary's Turn
Gary's Paldean Tauros used Raging Charge on Ash's Raichu for 200 damage.
Gary's Paldean Tauros used Raging Charge on Ash's Raichu for 200 damage.
Gary's Miltank used Tackle on Ash's Pikachu for 20 damage.
Gary took a Prize card.
"""


def test_opening_hand_is_the_one_kept_after_a_mulligan() -> None:
    assert battlelog.parse(LONGER).opening_hand() == ["Raichu", "Iono", "Basic Lightning Energy"]
    assert battlelog.parse(LOG).opening_hand()[:2] == ["Buddy-Buddy Poffin", "Budew"]


def test_prize_race_counts_per_turn() -> None:
    race = battlelog.parse(LONGER).prize_race()

    assert race == [
        {"turn": 1, "owner": "you", "you": 2, "opponent": 0},
        {"turn": 2, "owner": "opponent", "you": 2, "opponent": 1},
    ]


def test_several_item_lines_under_one_detail() -> None:
    attack = battlelog.parse(LONGER).turns[0].actions[0]

    assert attack.details == ["Damage breakdown: Base damage: 100 damage, Weakness: 20 damage"]


def test_deck_name_from_the_main_attackers() -> None:
    log = battlelog.parse(LONGER)

    # the main attacker; Miltank attacked once and is left out
    assert log.deck_name("Gary") == "Paldean Tauros"
    assert log.deck_name("Ash") == "Raichu"
    # nobody attacked in this one: fall back to what was played
    quiet = battlelog.parse(LOG)
    assert quiet.deck_name(quiet.opponent) == "Dhelmise / Poltchageist"


EVOLVING = """Setup
Ash drew 7 cards for the opening hand.
- 7 drawn cards.
   • Pikachu, Iono

Gary's Turn
Gary played Abra to the Bench.
Gary evolved Abra to Kadabra on the Bench.
Gary's Kadabra used Psyshot on Ash's Pikachu for 30 damage.
Gary's Kadabra used Psyshot on Ash's Pikachu for 30 damage.

Ash's Turn
Ash drew Nest Ball.
Ash played Nest Ball.
- Ash drew Raichu and played it to the Bench.
Ash attached Basic Lightning Energy to Pikachu in the Active Spot.
Ash played Iono.
- Ash drew 2 cards: Switch, Pikachu

Gary's Turn
Gary evolved Kadabra to Alakazam on the Bench.
Gary evolved Duskull to Dusclops on the Bench.
"""


def test_deck_is_named_after_its_final_evolutions() -> None:
    log = battlelog.parse(EVOLVING)

    # Kadabra did all the attacking, but the deck is an Alakazam deck
    assert log.deck_name("Gary") == "Alakazam / Dusclops"


def test_cards_played_and_seen() -> None:
    log = battlelog.parse(EVOLVING)

    assert log.cards_played("Ash") == {
        "Nest Ball": 1,
        "Raichu": 1,
        "Basic Lightning Energy": 1,
        "Iono": 1,
    }
    # also what was only drawn or sat in the opening hand
    assert log.cards_seen("Ash") == {
        "Pikachu", "Iono", "Nest Ball", "Raichu", "Basic Lightning Energy", "Switch",
    }
    assert log.cards_played("Gary") == {"Abra": 1, "Kadabra": 1, "Alakazam": 1, "Dusclops": 1}


def test_first_prize_setup_turn_and_turn_activity() -> None:
    log = battlelog.parse(LONGER)

    assert log.first_prize() == (1, "you")
    assert battlelog.parse(LOG).first_prize() is None

    real = battlelog.parse(LOG)
    assert real.final_forms(real.me) == ["Drakloak", "Dudunsparce"]
    assert real.first_in_play(real.me, "Drakloak") == 2  # my second turn
    assert real.first_in_play(real.me, "Dragapult ex") is None
    assert real.turn_activity(real.me) == [
        {"turn": 1, "attached": True, "attacked": True},
        {"turn": 2, "attached": False, "attacked": False},
    ]
