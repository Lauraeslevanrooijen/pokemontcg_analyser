import json
from pathlib import Path

import httpx
import pytest

from pokemontcg_analyser import cards


def _make_handler(pages: dict[int, list[dict]]):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": pages.get(page, [])})

    return handler


def test_fetch_all_cards_paginates_until_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        1: [{"id": f"card-{i}"} for i in range(3)],
        2: [{"id": "card-3"}],  # shorter than page_size -> stop here
    }
    transport = httpx.MockTransport(_make_handler(pages))

    def fake_client() -> httpx.Client:
        return httpx.Client(base_url=cards.API_BASE, transport=transport)

    monkeypatch.setattr(cards, "_client", fake_client)

    result = cards.fetch_all_cards(page_size=3)

    assert [c["id"] for c in result] == ["card-0", "card-1", "card-2", "card-3"]


def test_get_with_retries_recovers_from_transient_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json={"data": [{"id": "ok"}]})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cards, "_RETRY_BASE_DELAY_SECONDS", 0.001)
    client = httpx.Client(base_url=cards.API_BASE, transport=transport)

    resp = cards._get_with_retries(client, "/cards", {"page": 1, "pageSize": 1})

    assert resp.json() == {"data": [{"id": "ok"}]}
    assert attempts["n"] == 3


def test_sync_and_load_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {1: [{"id": "card-0", "name": "Pikachu"}]}
    transport = httpx.MockTransport(_make_handler(pages))
    monkeypatch.setattr(
        cards, "_client", lambda: httpx.Client(base_url=cards.API_BASE, transport=transport)
    )
    cache_path = tmp_path / "cards.json"

    count = cards.sync_cache(cache_path=cache_path)
    assert count == 1

    loaded = cards.load_cache(cache_path=cache_path)
    assert loaded == [{"id": "card-0", "name": "Pikachu"}]


def test_load_cache_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        cards.load_cache(cache_path=tmp_path / "missing.json")
