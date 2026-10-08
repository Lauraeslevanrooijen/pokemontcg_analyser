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
