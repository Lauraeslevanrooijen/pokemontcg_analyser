"""Decklists: parsing the text Pokémon TCG Live exports, and comparing two
versions of a list."""

from __future__ import annotations

import re
from dataclasses import dataclass

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
