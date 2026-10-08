"""Read the battle log Pokémon TCG Live lets you copy after a match.

The log is plain text: a "Setup" part, then one block per turn headed
"<Player>'s Turn", each line an action with indented "- " and "•" lines
giving its details, and a last line saying who won. It has no timestamps,
so it says what happened in each turn but not when.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# "Brock's Turn"; some versions of the game number them: "Turn # 3 - X's Turn".
_TURN_HEADER = re.compile(r"^(?:Turn\s*#\s*\d+\s*-\s*)?(.+?)'s Turn$")
_OPENING_HAND = re.compile(r"^(.+?) drew \d+ cards for the opening hand\.$")
_WENT = re.compile(r"^(.+?) decided to go (first|second)\.$")
_WINS = re.compile(r"([^.]+?) wins\.\s*$")
_SETUP_ACTOR = re.compile(r"^(.+?) (?:chose|won the coin toss|decided to go|drew \d+ cards)\b")
# Where a Pokémon is named in an action, to list what the opponent played.
_PRIZES = re.compile(r"^(.+?) took (a|\d+) Prize cards?\.")
_ATTACK = re.compile(r"^(.+?)'s (.+?) used .+? on .+? for \d+ damage")
_EVOLVED = re.compile(r"^(.+?) evolved (.+?) to (.+?) (?:on the Bench|in the Active Spot)")
_PLAYED = re.compile(r"^(.+?) played (.+?)(?: to the (?:Active Spot|Bench|Stadium spot))?\.$")
_ATTACHED = re.compile(r"^(.+?) attached (.+?) to ")
_DREW_ONE = re.compile(
    r"^(.+?) drew (?!a card\b|\d+ cards?\b)(.+?)( and played it to the Bench)?\.$"
)
_LISTED = re.compile(r"^(.+?) (?:drew|discarded) \d+ cards?(?: and played them to the Bench)?: (.+)$")
_POKEMON = [
    re.compile(r"played (.+?) to the (?:Active Spot|Bench)"),
    re.compile(r"evolved .+? to (.+?) (?:on the Bench|in the Active Spot)"),
    re.compile(r"'s (.+?) used "),
]


@dataclass
class Action:
    text: str
    details: list[str] = field(default_factory=list)


@dataclass
class LogTurn:
    player: str
    actions: list[Action] = field(default_factory=list)


@dataclass
class BattleLog:
    players: list[str] = field(default_factory=list)
    me: str | None = None  # the player whose drawn cards the log names
    first_player: str | None = None
    winner: str | None = None
    setup: list[Action] = field(default_factory=list)
    turns: list[LogTurn] = field(default_factory=list)

    @property
    def opponent(self) -> str | None:
        return next((p for p in self.players if p != self.me), None) if self.me else None

    @property
    def turn_order(self) -> str | None:
        """Whether I went "first" or "second", if the log says."""
        if not self.me or not self.first_player:
            return None
        return "first" if self.first_player == self.me else "second"

    @property
    def result(self) -> str | None:
        if not self.me or not self.winner:
            return None
        return "win" if self.winner == self.me else "loss"

    def pokemon_of(self, player: str | None) -> list[str]:
        """The Pokémon a player put into play or attacked with, in order."""
        found: list[str] = []
        if not player:
            return found
        for action in self.setup + [a for turn in self.turns for a in turn.actions]:
            if not action.text.startswith(player):
                continue
            for pattern in _POKEMON:
                hit = pattern.search(action.text)
                if hit and hit.group(1) not in found:
                    found.append(hit.group(1))
        return found

    def opening_hand(self) -> list[str]:
        """The cards I started with. After a mulligan the log has several
        opening hands; the last one is the hand that was kept."""
        hand: list[str] = []
        for action in self.setup:
            drew = _OPENING_HAND.match(action.text)
            if drew and drew.group(1) == self.me:
                for detail in action.details:
                    if ": " in detail:
                        hand = [card.strip() for card in detail.split(": ", 1)[1].split(",")]
        return hand

    def prize_race(self) -> list[dict]:
        """Per turn, how many Prize cards each side has taken so far."""
        mine = theirs = 0
        race = []
        for number, turn in enumerate(self.turns, start=1):
            for action in turn.actions:
                took = _PRIZES.match(action.text)
                if not took:
                    continue
                count = 1 if took.group(2) == "a" else int(took.group(2))
                if took.group(1) == self.me:
                    mine += count
                else:
                    theirs += count
            race.append(
                {
                    "turn": number,
                    "owner": "you" if turn.player == self.me else "opponent",
                    "you": mine,
                    "opponent": theirs,
                }
            )
        return race

    def cards_played(self, player: str | None) -> dict[str, int]:
        """How often a player put each card to use: played it, attached it,
        or evolved into it."""
        played: dict[str, int] = {}

        def count(card: str) -> None:
            played[card] = played.get(card, 0) + 1

        for action in self._actions():
            for pattern, group in ((_EVOLVED, 3), (_ATTACHED, 2), (_PLAYED, 2)):
                hit = pattern.match(action.text)
                if hit:
                    if hit.group(1) == player:
                        count(hit.group(group))
                    break
            for detail in action.details:
                # "drew 2 cards and played them to the Bench: Dreepy, Dreepy"
                hit = _LISTED.match(detail)
                if hit and hit.group(1) == player and "played them" in detail:
                    for card in hit.group(2).split(","):
                        count(card.strip())
                one = _DREW_ONE.match(detail)
                if one and one.group(1) == player and one.group(3):
                    count(one.group(2))
        return played

    def cards_seen(self, player: str | None) -> set[str]:
        """Every card the log shows to be in a player's deck: played, or
        named when drawn or discarded from hand."""
        seen = set(self.cards_played(player))
        if player == self.me:
            seen.update(self.opening_hand())
        for action in self._actions():
            for line in [action.text, *action.details]:
                one = _DREW_ONE.match(line)
                if one and one.group(1) == player:
                    seen.add(one.group(2))
                many = _LISTED.match(line)
                if many and many.group(1) == player:
                    seen.update(card.strip() for card in many.group(2).split(","))
        return seen

    def _actions(self) -> list[Action]:
        return self.setup + [a for turn in self.turns for a in turn.actions]

    def deck_name(self, player: str | None) -> str | None:
        """A name for a player's deck from what it did.

        Decks are named after the fully evolved Pokémon they are built
        around, so the ends of the evolution lines seen in the log come
        first (Alakazam, not the Kadabra that did the early attacking). A
        deck that evolved nothing is named after the Pokémon that attacked
        most, plus a second one if it did a real share of the attacking,
        or else the first two that were played. Alphabetical, and without
        one-off attackers, so the same deck tends to get the same name from
        one game to the next."""
        if not player:
            return None
        evolved_from, evolved_to = set(), []
        for action in self._actions():
            hit = _EVOLVED.match(action.text)
            if hit and hit.group(1) == player:
                evolved_from.add(hit.group(2))
                if hit.group(3) not in evolved_to:
                    evolved_to.append(hit.group(3))
        final_forms = [name for name in evolved_to if name not in evolved_from]
        if final_forms:
            return " / ".join(sorted(final_forms[:2]))
        attacks: dict[str, int] = {}
        for turn in self.turns:
            for action in turn.actions:
                hit = _ATTACK.match(action.text)
                if hit and hit.group(1) == player:
                    attacks[hit.group(2)] = attacks.get(hit.group(2), 0) + 1
        ranked = sorted(attacks, key=lambda name: (-attacks[name], name))
        main = ranked[:1]
        if len(ranked) > 1 and attacks[ranked[1]] >= max(2, attacks[ranked[0]] / 3):
            main.append(ranked[1])
        return " / ".join(sorted(main or self.pokemon_of(player)[:2])) or None

    def narrated(self, turn: LogTurn) -> list[Action]:
        """A turn's actions worded for reading beside the video: the turn
        owner's name dropped from the front, the players called you and
        opponent."""
        return [self._reword(action, turn.player) for action in turn.actions]

    def _reword(self, action: Action, owner: str | None) -> Action:
        def reword(text: str, strip_owner: bool) -> str:
            if strip_owner and owner:
                for lead in (f"{owner}'s ", f"{owner} "):
                    if text.startswith(lead):
                        text = text[len(lead):]
                        break
            for player in self.players:
                name = "you" if player == self.me else "opponent"
                text = text.replace(f"{player} wins", "You win" if player == self.me else "Opponent wins")
                text = text.replace(f"{player}'s", "your" if player == self.me else "opponent's")
                text = re.sub(rf"\b{re.escape(player)}\b", name, text)
            return text[:1].upper() + text[1:]

        return Action(reword(action.text, True), [reword(d, True) for d in action.details])


def parse(text: str) -> BattleLog:
    log = BattleLog()
    actions = log.setup
    last: Action | None = None
    itemised = False  # whether the detail being read already has its items
    for raw in text.replace("’", "'").splitlines():
        line = raw.strip()
        if not line or line == "Setup":
            continue
        header = _TURN_HEADER.match(line)
        if header:
            player = header.group(1)
            if player not in log.players:
                log.players.append(player)
            log.turns.append(LogTurn(player))
            actions, last = log.turns[-1].actions, None
            continue
        if line.startswith("•"):
            # what an indented "- 7 drawn cards." line is about; a detail
            # can have several of these lines
            if last is not None:
                items = line.lstrip("• ").strip()
                if not last.details:
                    last.details.append(items)
                elif itemised:
                    last.details[-1] = f"{last.details[-1]}, {items}"
                else:
                    last.details[-1] = f"{last.details[-1].rstrip('.:')}: {items}"
                itemised = True
            continue
        itemised = False
        if line.startswith("- "):
            if last is not None:
                last.details.append(line[2:].strip())
            continue
        last = Action(line)
        actions.append(last)

    went_second: str | None = None
    for action in log.setup:
        actor = _SETUP_ACTOR.match(action.text)
        if actor and actor.group(1) not in log.players:
            log.players.append(actor.group(1))
        went = _WENT.match(action.text)
        if went and went.group(2) == "first":
            log.first_player = went.group(1)
        elif went:
            went_second = went.group(1)
        hand = _OPENING_HAND.match(action.text)
        # Only your own opening hand is spelled out, card by card.
        if hand and any(":" in detail for detail in action.details):
            log.me = hand.group(1)

    # "X decided to go second" names the other player as first, who may
    # only become known from a later line.
    if log.first_player is None and went_second:
        log.first_player = next((p for p in log.players if p != went_second), None)
    if log.first_player is None and log.turns:
        log.first_player = log.turns[0].player

    everything = log.setup + [a for turn in log.turns for a in turn.actions]
    for action in reversed(everything):
        winner = _WINS.search(action.text)
        if winner:
            name = winner.group(1).strip()
            log.winner = next((p for p in log.players if p == name), None)
            break
    return log
