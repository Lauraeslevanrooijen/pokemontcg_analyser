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
