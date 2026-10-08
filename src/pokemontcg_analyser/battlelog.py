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
            # the cards an indented "- 7 drawn cards." line is about
            if last is not None:
                cards = line.lstrip("• ").strip()
                if last.details:
                    last.details[-1] = f"{last.details[-1].rstrip('.')}: {cards}"
                else:
                    last.details.append(cards)
            continue
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
