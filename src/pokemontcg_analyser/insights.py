"""Cross-match views of what has been logged and annotated: win rates,
and where the labelled moments (misplays, ...) fall."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .storage import LABELS, Event, Match


@dataclass
class Record:
    wins: int = 0
    losses: int = 0
    ties: int = 0

    def add(self, result: str) -> None:
        if result == "win":
            self.wins += 1
        elif result == "loss":
            self.losses += 1
        else:
            self.ties += 1

    @property
    def total(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def win_rate(self) -> float:
        return self.wins / self.total if self.total else 0.0


@dataclass
class Moment:
    """A labelled note, with the match it belongs to."""

    match: Match
    event: Event
    turn: int | None  # which turn it fell in; None when the match has no turn markers


@dataclass
class Insights:
    overall: Record = field(default_factory=Record)
    by_deck: dict[str, Record] = field(default_factory=dict)
    by_matchup: dict[tuple[str, str], Record] = field(default_factory=dict)
    by_turn_order: dict[str, Record] = field(default_factory=dict)
    # Annotated matches only: label -> how many notes carry it.
    label_counts: dict[str, int] = field(default_factory=dict)
    reviewed_matches: int = 0
    # result -> (misplays, reviewed matches with that result)
    misplays_by_result: dict[str, tuple[int, int]] = field(default_factory=dict)
    # turn number -> misplays in that turn, across matches with turn markers
    misplays_by_turn: dict[int, int] = field(default_factory=dict)


def turn_at(events: list[Event], offset: float | None) -> int | None:
    """The turn a moment falls in: how many turn markers precede it. None if
    the match has no turn markers (or the moment has no position)."""
    turns = [e.video_offset_seconds for e in events if e.kind == "turn"]
    if not turns or offset is None:
        return None
    return sum(1 for start in turns if start is not None and start <= offset)


def moments(
    matches: list[Match], events: list[Event], label: str | None = None
) -> list[Moment]:
    """Every labelled note (optionally one label), newest match first and
    in playing order within a match."""
    by_match: dict[int, list[Event]] = defaultdict(list)
    for event in events:
        by_match[event.match_id].append(event)
    found = []
    for match in matches:  # list_matches is newest first
        match_events = by_match.get(match.id, [])
        for event in match_events:
            if event.label is None or (label is not None and event.label != label):
                continue
            found.append(Moment(match, event, turn_at(match_events, event.video_offset_seconds)))
    return found


def build(matches: list[Match], events: list[Event]) -> Insights:
    insights = Insights()
    for label in LABELS:
        insights.label_counts[label] = 0
    for match in matches:
        insights.overall.add(match.result)
        insights.by_deck.setdefault(match.deck, Record()).add(match.result)
        matchup = (match.deck, match.opponent_deck or "?")
        insights.by_matchup.setdefault(matchup, Record()).add(match.result)
        order = match.turn_order or "unknown"
        insights.by_turn_order.setdefault(order, Record()).add(match.result)

    by_match: dict[int, list[Event]] = defaultdict(list)
    for event in events:
        by_match[event.match_id].append(event)
    misplays_by_result: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for match in matches:
        match_events = by_match.get(match.id, [])
        # Unreviewed matches would drag every per-game average toward zero.
        if not any(e.kind == "note" for e in match_events):
            continue
        insights.reviewed_matches += 1
        misplays_by_result[match.result][1] += 1
        for event in match_events:
            if event.label in insights.label_counts:
                insights.label_counts[event.label] += 1
            if event.label != "misplay":
                continue
            misplays_by_result[match.result][0] += 1
            turn = turn_at(match_events, event.video_offset_seconds)
            if turn:
                insights.misplays_by_turn[turn] = insights.misplays_by_turn.get(turn, 0) + 1
    insights.misplays_by_result = {k: (v[0], v[1]) for k, v in misplays_by_result.items()}
    insights.misplays_by_turn = dict(sorted(insights.misplays_by_turn.items()))
    return insights


@dataclass
class Openings:
    """What opening hands looked like across the matches with a battle log."""

    games: int = 0
    # card name -> record of the games it was in the opening hand
    by_card: dict[str, Record] = field(default_factory=dict)
    # a property of the hand ("A Supporter") -> record with it / without it
    by_feature: dict[str, tuple[Record, Record]] = field(default_factory=dict)
    # kind of card ("Basic Pokémon") -> average number in the opening hand
    average: dict[str, float] = field(default_factory=dict)
    typed_games: int = 0  # games where the kinds of the cards were known


# name, and how it reads off the number of each kind of card in a hand
HAND_FEATURES = [
    ("A Supporter in hand", lambda kinds: kinds.get("Supporter", 0) >= 1),
    ("Only one Basic Pokémon", lambda kinds: kinds.get("Basic Pokémon", 0) == 1),
    ("Two or more Energy", lambda kinds: kinds.get("Energy", 0) >= 2),
]


def openings(hands: list[tuple[str, list[str], dict[str, str]]]) -> Openings:
    """`hands` is one entry per match with a battle log: its result, the
    cards in the opening hand, and what kind each of those cards is (empty
    when the deck's list isn't saved, so kinds are unknown)."""
    found = Openings(games=len(hands))
    totals: dict[str, int] = defaultdict(int)
    features = {name: (Record(), Record()) for name, _ in HAND_FEATURES}
    for result, hand, kinds in hands:
        for card in dict.fromkeys(hand):
            found.by_card.setdefault(card, Record()).add(result)
        # Only hands where every card's kind is known say anything about
        # "no Supporter" and the like.
        if not hand or any(card not in kinds for card in hand):
            continue
        found.typed_games += 1
        counts: dict[str, int] = defaultdict(int)
        for card in hand:
            counts[kinds[card]] += 1
            totals[kinds[card]] += 1
        for name, test in HAND_FEATURES:
            features[name][0 if test(counts) else 1].add(result)
    if found.typed_games:
        found.average = {kind: totals[kind] / found.typed_games for kind in sorted(totals)}
        found.by_feature = features
    found.by_card = dict(
        sorted(found.by_card.items(), key=lambda item: (-item[1].total, item[0]))
    )
    return found
