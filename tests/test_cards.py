from pathlib import Path

import httpx
import pytest

from pokemontcg_analyser import cards


@pytest.fixture()
def tcgdex(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A stand-in TCGdex with one set (TWM = sv06) holding one card.
    Returns the list of paths requested, to check what was cached."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v2/en")
        requested.append(path)
        if path == "/sets":
            return httpx.Response(200, json=[{"id": "sv06"}, {"id": "A1"}])
        if path == "/sets/sv06":
            return httpx.Response(200, json={"id": "sv06", "abbreviation": {"official": "TWM"}})
        if path == "/sets/A1":
            return httpx.Response(200, json={"id": "A1"})  # a set without a code
        if path == "/sets/sv06/130":
            return httpx.Response(
                200,
                json={"id": "sv06-130", "name": "Dragapult ex", "image": "https://assets.example/sv06/130"},
            )
        if path == "/sets/sv06/131":  # a card TCGdex has no scan of
            return httpx.Response(200, json={"id": "sv06-131", "name": "Fire Energy"})
        if path == "/cards":
            assert request.url.params["name"] == "eq:Fire Energy"
            return httpx.Response(
                200,
                json=[
                    {"id": "sm1-165", "image": "https://assets.example/sm1/165"},
                    {"id": "swsh8-284", "image": "https://assets.example/swsh8/284"},
                    {"id": "swsh8-285", "image": "https://assets.example/swsh8/285"},
                    {"id": "sv06-131"},
                    {"id": "B2-189", "image": "https://assets.example/pocket"},
                ],
            )
        if request.url.path == "/sv06/130/low.webp":
            return httpx.Response(200, content=b"webp-bytes")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cards, "_client", lambda: httpx.Client(base_url=cards.API_BASE, transport=transport))
    monkeypatch.setattr(cards, "_image_client", lambda: httpx.Client(transport=transport))
    monkeypatch.setattr(cards, "_unknown_codes", set())
    return requested


def test_lookup_resolves_a_decklist_printing_and_caches_it(tmp_path: Path, tcgdex: list[str]) -> None:
    card = cards.lookup("TWM", "130", tmp_path)

    assert card == cards.Card("sv06-130", "Dragapult ex", "https://assets.example/sv06/130")
    tcgdex.clear()
    assert cards.lookup("TWM", "130", tmp_path) == card
    assert tcgdex == []  # second time straight from disk


def test_lookup_of_unknown_set_or_card(tmp_path: Path, tcgdex: list[str]) -> None:
    assert cards.lookup("TWM", "999", tmp_path) is None
    assert cards.lookup("XXX", "1", tmp_path) is None
    tcgdex.clear()
    # an unknown code doesn't refetch the set list every time it is asked
    assert cards.lookup("XXX", "2", tmp_path) is None
    assert tcgdex == []


def test_image_is_downloaded_once(tmp_path: Path, tcgdex: list[str]) -> None:
    path = cards.image_path("TWM", "130", tmp_path)

    assert path == tmp_path / "images" / "sv06-130.webp"
    assert path.read_bytes() == b"webp-bytes"
    tcgdex.clear()
    assert cards.image_path("TWM", "130", tmp_path) == path
    assert tcgdex == []
    assert cards.image_path("TWM", "999", tmp_path) is None


def test_sync_counts_cards_with_a_picture(tmp_path: Path, tcgdex: list[str]) -> None:
    assert cards.sync([("TWM", "130"), ("TWM", "130"), ("TWM", "999")], tmp_path) == 1


def test_card_without_a_scan_borrows_another_printing(tmp_path: Path, tcgdex: list[str]) -> None:
    card = cards.lookup("TWM", "131", tmp_path)

    # first card of the newest main-game set that has one; never TCG Pocket
    assert card.image == "https://assets.example/swsh8/284"


def test_images_by_name_takes_the_newest_main_game_printings(tmp_path: Path, tcgdex: list[str]) -> None:
    pictures = cards.images_by_name("Fire Energy", limit=2, cache_dir=tmp_path)

    # the last two with a picture, and never the TCG Pocket one; only the
    # one the stand-in server has a file for could be downloaded
    assert [p.name for p in pictures] == []
    tcgdex.clear()
    cards.images_by_name("Fire Energy", limit=2, cache_dir=tmp_path)
    assert "/cards" not in tcgdex  # the lookup itself is cached
