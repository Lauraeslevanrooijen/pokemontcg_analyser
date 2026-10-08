from pathlib import Path

import cv2
import numpy as np

from pokemontcg_analyser import logtimes


def _card(tmp_path: Path, name: str, seed: int) -> Path:
    """A random, card-shaped picture: distinct from any other seed's."""
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 255, size=(337, 245), dtype=np.uint8)
    path = tmp_path / f"{name}.png"
    cv2.imwrite(str(path), cv2.GaussianBlur(image, (0, 0), 6))
    return path


def _frame(card: Path | None) -> np.ndarray:
    """The game picture, with a card shown enlarged if given."""
    frame = np.full((270, 480), 40, dtype=np.uint8)
    if card is not None:
        shown = logtimes._templates(card)[1]  # at the size a played card has
        frame[80 : 80 + shown.shape[0], 200 : 200 + shown.shape[1]] = shown
    return frame


def test_plays_are_found_in_order(tmp_path: Path) -> None:
    ball, pad = _card(tmp_path, "ball", 1), _card(tmp_path, "pad", 2)
    # 2s nothing, 1s Ultra Ball, 2s nothing, 1s Poké Pad
    frames = [_frame(None)] * 8 + [_frame(ball)] * 4 + [_frame(None)] * 8 + [_frame(pad)] * 4

    found = logtimes._locate(frames, start=100.0, plays=[(1, ball), (3, pad)])

    assert found == {1: 102.0, 3: 105.0}


def test_a_play_that_never_shows_is_left_without_a_time(tmp_path: Path) -> None:
    ball, pad, hammer = (_card(tmp_path, n, i) for i, n in enumerate(["ball", "pad", "hammer"]))
    frames = [_frame(ball)] * 4 + [_frame(None)] * 4 + [_frame(pad)] * 4

    found = logtimes._locate(frames, start=0.0, plays=[(0, ball), (1, hammer), (2, pad)])

    assert found == {0: 0.0, 2: 2.0}


def test_two_copies_of_a_card_get_two_moments(tmp_path: Path) -> None:
    hammer = _card(tmp_path, "hammer", 3)
    frames = [_frame(hammer)] * 4 + [_frame(None)] * 4 + [_frame(hammer)] * 4

    found = logtimes._locate(frames, start=0.0, plays=[(0, hammer), (1, hammer)])

    assert found == {0: 0.0, 1: 2.0}
    # only one showing: the second copy has no moment of its own
    once = logtimes._locate(frames[:8], start=0.0, plays=[(0, hammer), (1, hammer)])
    assert once == {0: 0.0}
