"""Decklists: parsing the text Pokémon TCG Live exports, and comparing two
versions of a list."""

from __future__ import annotations

import re
from dataclasses import dataclass
from math import comb

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


# The set code and collector number Pokémon TCG Live appends: "TWM 130".
_PRINTING = re.compile(r"\s+[A-Z][A-Za-z0-9-]{1,5}\s+\d+[a-z]?$")


def card_names(decklist: str) -> list[str]:
    """Just the names in a list, without set codes: "Dragapult ex"."""
    return list(dict.fromkeys(_PRINTING.sub("", card) for card in parse(decklist)))


# How the game writes energy types in an export: "Basic {L} Energy".
_ENERGY_TYPES = {
    "G": "Grass", "R": "Fire", "W": "Water", "L": "Lightning", "P": "Psychic",
    "F": "Fighting", "D": "Darkness", "M": "Metal", "N": "Dragon", "C": "Colorless",
}
_ENERGY_SYMBOL = re.compile(r"\{([A-Z])\}")
# A section header: "Pokémon: 15". "Total Cards: 60" looks alike but isn't one.
_SECTION = re.compile(r"^\s*([^\d:][^:]*):\s*\d+\s*$")


@dataclass(frozen=True)
class ListedCard:
    count: int
    name: str  # readable: "Basic Lightning Energy"
    printing: str  # set code and number, "MEE 12"; empty if the line had none

    @property
    def set_code(self) -> str:
        return self.printing.split()[0] if self.printing else ""

    @property
    def number(self) -> str:
        return self.printing.split()[-1] if self.printing else ""


@dataclass(frozen=True)
class Section:
    title: str  # "Pokémon", "Trainer", "Energy"; empty for cards before any header
    cards: list[ListedCard]

    @property
    def total(self) -> int:
        return sum(card.count for card in self.cards)


def sections(decklist: str) -> list[Section]:
    """The list as the game groups it, for showing rather than comparing."""
    found: list[Section] = []
    for line in decklist.splitlines():
        header = _SECTION.match(line)
        if header:
            if not header.group(1).lower().startswith("total"):
                found.append(Section(header.group(1).strip(), []))
            continue
        card = _CARD_LINE.match(line)
        if not card:
            continue
        if not found:
            found.append(Section("", []))
        full = card.group(2)
        printing = _PRINTING.search(full)
        name = full[: printing.start()] if printing else full
        name = _ENERGY_SYMBOL.sub(lambda m: _ENERGY_TYPES.get(m.group(1), m.group(0)), name)
        found[-1].cards.append(
            ListedCard(int(card.group(1)), name, printing.group(0).strip() if printing else "")
        )
    return [section for section in found if section.cards]


HAND_SIZE = 7


def chance_of_none(copies: int, deck_size: int, hand: int = HAND_SIZE) -> float:
    """The chance that none of `copies` cards are among `hand` cards drawn
    from a deck of `deck_size`."""
    if deck_size < hand or copies <= 0:
        return 1.0 if copies <= 0 else 0.0
    return comb(deck_size - copies, hand) / comb(deck_size, hand)


def opening_odds(decklist: str, kinds: dict[str, str]) -> list[tuple[str, float]]:
    """What a list's opening seven cards look like, by chance alone.
    `kinds` says what each card is ("Basic Pokémon", "Supporter", ...);
    lines about a kind are left out when not every card's kind is known,
    as the count would be too low."""
    cards = parse_names(decklist)
    size = sum(cards.values())
    if size < HAND_SIZE:
        return []
    complete = all(name in kinds for name in cards)

    def total(kind: str) -> int:
        return sum(count for name, count in cards.items() if kinds.get(name) == kind)

    odds: list[tuple[str, float]] = []
    if complete:
        odds.append(("No Basic Pokémon (a mulligan)", chance_of_none(total("Basic Pokémon"), size)))
        odds.append(("No Supporter", chance_of_none(total("Supporter"), size)))
        odds.append(("No Energy", chance_of_none(total("Energy"), size)))
    basics = sorted(
        ((name, count) for name, count in cards.items() if kinds.get(name) == "Basic Pokémon"),
        key=lambda item: (-item[1], item[0]),
    )
    for name, count in basics:
        odds.append((f"At least one {name}", 1 - chance_of_none(count, size)))
    return odds


def parse_names(decklist: str) -> dict[str, int]:
    """Card name (without set code and number) -> copies, adding up
    different printings of the same card."""
    names: dict[str, int] = {}
    for section in sections(decklist):
        for card in section.cards:
            names[card.name] = names.get(card.name, 0) + card.count
    return names
