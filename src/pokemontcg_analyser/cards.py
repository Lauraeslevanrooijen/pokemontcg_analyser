"""Client for the public pokemontcg.io card database, with a local cache.

This is the foundation for card recognition later: having every card's name,
set, and image URL locally lets the analysis pipeline match against a known
set instead of guessing from scratch.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

API_BASE = "https://api.pokemontcg.io/v2"
DEFAULT_CACHE_PATH = Path("data/cards.json")

# The unauthenticated tier of this API is heavily rate-limited and prone to
# intermittent 500/502s under load. A free API key (see pokemontcg.io) raises
# the rate limit substantially; set POKEMONTCG_API_KEY to use one.
_MAX_RETRIES = 5
_RETRY_BASE_DELAY_SECONDS = 1.0


def _client() -> httpx.Client:
    headers = {}
    api_key = os.environ.get("POKEMONTCG_API_KEY")
    if api_key:
        headers["X-Api-Key"] = api_key
    return httpx.Client(base_url=API_BASE, timeout=30.0, headers=headers)


def _get_with_retries(client: httpx.Client, path: str, params: dict[str, Any]) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        resp = client.get(path, params=params)
        if resp.status_code < 500:
            resp.raise_for_status()
            return resp
        last_error = httpx.HTTPStatusError(
            f"{resp.status_code} from {resp.request.url}", request=resp.request, response=resp
        )
        time.sleep(_RETRY_BASE_DELAY_SECONDS * (2**attempt))
    assert last_error is not None
    raise last_error


def fetch_all_cards(page_size: int = 100) -> list[dict[str, Any]]:
    """Fetch the full card list from pokemontcg.io, paginating as needed.

    Retries transient 5xx errors (this API is flaky without an API key).
    """
    cards: list[dict[str, Any]] = []
    page = 1
    with _client() as client:
        while True:
            resp = _get_with_retries(client, "/cards", {"page": page, "pageSize": page_size})
            payload = resp.json()
            batch = payload.get("data", [])
            cards.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
    return cards


def sync_cache(cache_path: Path = DEFAULT_CACHE_PATH) -> int:
    """Fetch all cards and write them to the local JSON cache. Returns count."""
    cards = fetch_all_cards()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cards))
    return len(cards)


def load_cache(cache_path: Path = DEFAULT_CACHE_PATH) -> list[dict[str, Any]]:
    if not cache_path.exists():
        raise FileNotFoundError(
            f"No card cache at {cache_path}. Run `cards sync` first."
        )
    return json.loads(cache_path.read_text())
