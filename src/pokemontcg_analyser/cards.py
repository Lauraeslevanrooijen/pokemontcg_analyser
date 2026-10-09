"""Card data and images from TCGdex (https://tcgdex.dev), cached locally.

A card is identified the way Pokémon TCG Live writes it in a decklist or
battle log: a set code and a collector number, "TWM 130". TCGdex knows the
same set codes, so that pair resolves straight to a card and its image.
Everything fetched is kept under data/cards/, so each card is only looked
up once and the app keeps working offline for cards it has seen.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

API_BASE = "https://api.tcgdex.net/v2/en"
DEFAULT_CACHE_DIR = Path("data/cards")
# TCGdex serves each image as low/high in webp/png/jpg; low webp is ~25 KB
# at 245x337, plenty for a deck overview.
IMAGE_VARIANT = "low.webp"

_lock = threading.Lock()
# Set codes that were looked for and not found, so a typo in a decklist
# doesn't refetch the whole set list on every page load.
_unknown_codes: set[str] = set()


@dataclass(frozen=True)
class Card:
    id: str  # TCGdex id, "sv06-130"
    name: str
    image: str | None  # base URL; append "/low.webp" etc.
    category: str | None = None  # "Pokemon", "Trainer" or "Energy"
    stage: str | None = None  # "Basic", "Stage1", ... for Pokémon
    trainer_type: str | None = None  # "Supporter", "Item", "Tool", "Stadium"

    @property
    def kind(self) -> str | None:
        """What the card is for counting hands: "Basic Pokémon", "Evolution
        Pokémon", "Supporter", "Item", "Tool", "Stadium" or "Energy"."""
        if self.category == "Pokemon":
            return "Basic Pokémon" if self.stage == "Basic" else "Evolution Pokémon"
        if self.category == "Trainer":
            return self.trainer_type or "Trainer"
        return self.category


def _client() -> httpx.Client:
    return httpx.Client(base_url=API_BASE, timeout=30.0, follow_redirects=True)


def _image_client() -> httpx.Client:
    # Pictures live on a different host than the API.
    return httpx.Client(timeout=30.0, follow_redirects=True)


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True))


def fetch_set_codes() -> dict[str, str]:
    """Every set's official code ("TWM") mapped to its TCGdex id ("sv06").

    The set list itself doesn't carry the codes, so each set is asked for
    individually (about 220 small requests, a second or two in parallel).
    """
    with _client() as client:
        set_ids = [s["id"] for s in client.get("/sets").raise_for_status().json()]

        def code_of(set_id: str) -> tuple[str | None, str]:
            try:
                detail = client.get(f"/sets/{set_id}").raise_for_status().json()
            except httpx.HTTPError:
                return None, set_id
            return (detail.get("abbreviation") or {}).get("official"), set_id

        with ThreadPoolExecutor(8) as pool:
            pairs = list(pool.map(code_of, set_ids))
    codes: dict[str, str] = {}
    for code, set_id in pairs:
        # A few codes are shared by two sets; the first listed wins.
        if code and code not in codes:
            codes[code] = set_id
    return codes


def set_id_for(code: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> str | None:
    path = cache_dir / "sets.json"
    with _lock:
        codes = _read(path)
        if code in codes:
            return codes[code]
        if code in _unknown_codes:
            return None
        # Not seen before: a new set may have come out since the last fetch.
        try:
            codes = fetch_set_codes()
        except httpx.HTTPError:
            return None
        _write(path, codes)
        if code not in codes:
            _unknown_codes.add(code)
        return codes.get(code)


def _picture_of_another_printing(client: httpx.Client, name: str) -> str | None:
    """TCGdex has no scan for some cards (the Mega Evolution basic energies,
    some promos). The same card from another set is a different print but
    recognisably the same card, which beats an empty tile."""
    printings = client.get("/cards", params={"name": f"eq:{name}"}).raise_for_status().json()
    # Ids of the main card game's sets start lowercase ("sv06-130"); the
    # rest are from Pokémon TCG Pocket, a different game with other cards.
    usable = [p for p in printings if p.get("image") and p["id"][:1].islower()]
    if not usable:
        return None
    newest_set = usable[-1]["id"].rsplit("-", 1)[0]
    return next(p["image"] for p in usable if p["id"].rsplit("-", 1)[0] == newest_set)


def lookup(set_code: str, number: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Card | None:
    """The card a decklist line such as "TWM 130" refers to, or None if
    TCGdex doesn't have it (or can't be reached and it isn't cached)."""
    key = f"{set_code} {number}"
    path = cache_dir / "cards.json"
    with _lock:
        cached = _read(path).get(key)
    # Entries cached before card types were kept are fetched once more.
    if cached and "category" in cached:
        return Card(**cached)
    set_id = set_id_for(set_code, cache_dir)
    if set_id is None:
        return None
    try:
        with _client() as client:
            resp = client.get(f"/sets/{set_id}/{number}")
            if resp.status_code == 404:
                return None
            data = resp.raise_for_status().json()
            image = data.get("image") or _picture_of_another_printing(client, data["name"])
    except httpx.HTTPError:
        return None
    card = Card(
        id=data["id"],
        name=data["name"],
        image=image,
        category=data.get("category"),
        stage=data.get("stage"),
        trainer_type=data.get("trainerType"),
    )
    with _lock:
        cards = _read(path)
        cards[key] = asdict(card)
        _write(path, cards)
    return card


def image_path(set_code: str, number: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path | None:
    """A local file with the card's picture, downloading it the first time."""
    card = lookup(set_code, number, cache_dir)
    if card is None or not card.image:
        return None
    path = cache_dir / "images" / f"{card.id}.webp"
    if path.exists():
        return path
    try:
        with _image_client() as client:
            content = client.get(f"{card.image}/{IMAGE_VARIANT}").raise_for_status().content
    except httpx.HTTPError:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def sync(printings: list[tuple[str, str]], cache_dir: Path = DEFAULT_CACHE_DIR) -> int:
    """Fetch data and images for these (set code, number) pairs ahead of
    time. Returns how many have a picture afterwards."""
    with ThreadPoolExecutor(8) as pool:
        paths = list(pool.map(lambda p: image_path(p[0], p[1], cache_dir), dict.fromkeys(printings)))
    return sum(path is not None for path in paths)


def images_by_name(name: str, limit: int = 5, cache_dir: Path = DEFAULT_CACHE_DIR) -> list[Path]:
    """Pictures of a card known only by name, as for an opponent's cards in
    a battle log: its newest few printings in the main card game, any of
    which may be the one that was played. Empty when it can't be found."""
    path = cache_dir / "cards.json"
    key = f"name:{name}"
    with _lock:
        known = _read(path).get(key)
    if known is None:
        try:
            with _client() as client:
                printings = client.get("/cards", params={"name": f"eq:{name}"}).raise_for_status().json()
        except httpx.HTTPError:
            return []
        usable = [p for p in printings if p.get("image") and p["id"][:1].islower()]
        known = [{"id": p["id"], "image": p["image"]} for p in usable[-limit:]]
        with _lock:
            cached = _read(path)
            cached[key] = known
            _write(path, cached)
    pictures = []
    for printing in known:
        target = cache_dir / "images" / f"{printing['id']}.webp"
        if not target.exists():
            try:
                with _image_client() as client:
                    content = client.get(f"{printing['image']}/{IMAGE_VARIANT}").raise_for_status().content
            except httpx.HTTPError:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        pictures.append(target)
    return pictures
