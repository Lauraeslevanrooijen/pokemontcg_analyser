"""Decklists: parsing the text Pokémon TCG Live exports, and comparing two
versions of a list."""

from __future__ import annotations

import re

# A card line is "<count> <name ...>", e.g. "4 Dragapult ex TWM 130".
# Section headers such as "Pokémon: 12" start with a word and are skipped.
_CARD_LINE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")


def parse(decklist: str) -> dict[str, int]:
    """Card name (including its set code and number) -> how many copies."""
    cards: dict[str, int] = {}
    for line in decklist.splitlines():
        found = _CARD_LINE.match(line)
        if found:
            name = found.group(2)
            cards[name] = cards.get(name, 0) + int(found.group(1))
    return cards


def card_count(decklist: str) -> int:
    return sum(parse(decklist).values())


def diff(old: str, new: str) -> list[tuple[int, str]]:
    """What changed going from `old` to `new`: (change in copies, card),
    additions first, then removals."""
    before, after = parse(old), parse(new)
    changes = [
        (after.get(card, 0) - before.get(card, 0), card)
        for card in sorted(set(before) | set(after))
        if after.get(card, 0) != before.get(card, 0)
    ]
    return sorted(changes, key=lambda change: (change[0] < 0, change[1]))
