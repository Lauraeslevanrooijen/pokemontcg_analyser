"""Find when the plays in a battle log happen in the recording.

The log says what was played in each turn but not when. When a card is
played, the game shows it enlarged for a moment; this looks for that
enlarged picture, card by card, inside the turn it was played in. Each
play is searched for after the one before it, so two copies of a card, or
a card that was also on screen earlier, land on the right moment.

Tried on one turn of a real match: six of seven plays found, in the order
the log gives them; the seventh scored too low and was left without a
time rather than guessed.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from . import turns

SAMPLE_FPS = 4
# The enlarged card is about 82 of the picture's 480 pixels wide; cards
# shown while searching the deck are a little smaller.
CARD_WIDTHS = (74, 82)
CARD_ASPECT = 0.716  # width / height of a card
# Normalised correlation of the card picture against the frame. Played
# cards score 0.76-0.88; other cards that happen to be on screen reach
# about 0.64.
MIN_SCORE = 0.72

Play = tuple[int, Path]  # the action's position in its turn, and the card's picture


def _templates(image_path: Path) -> list[np.ndarray]:
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return []
    return [
        cv2.resize(image, (width, round(width / CARD_ASPECT)), interpolation=cv2.INTER_AREA)
        for width in CARD_WIDTHS
    ]


def _locate(frames: list[np.ndarray], start: float, plays: list[Play]) -> dict[int, float]:
    """Times of one turn's plays, given that turn's frames (grayscale)."""
    found: dict[int, float] = {}
    earliest = 0  # frame index the next play is searched from
    previous: Path | None = None  # the card found last
    for action, image_path in plays:
        templates = _templates(image_path)
        if not templates:
            continue

        def shows(index: int) -> bool:
            return any(
                float(cv2.matchTemplate(frames[index], template, cv2.TM_CCOEFF_NORMED).max())
                >= MIN_SCORE
                for template in templates
            )

        index = earliest
        if image_path == previous:
            # A second copy of the card just found: wait for that one to
            # leave the screen, or both would land on the same moment.
            while index < len(frames) and shows(index):
                index += 1
        while index < len(frames) and not shows(index):
            index += 1
        if index < len(frames):
            found[action] = start + index / SAMPLE_FPS
            earliest, previous = index, image_path
    return found


def find_play_times(
    video_path: Path,
    windows: list[tuple[float, float]],
    plays: list[list[Play]],
) -> dict[tuple[int, int], float]:
    """`windows[i]` is when turn i+1 runs in the video and `plays[i]` its
    card plays in log order. Returns {(turn number, action): seconds} for
    the plays that were found."""
    times: dict[tuple[int, int], float] = {}
    wanted = [i for i, turn_plays in enumerate(plays) if turn_plays and i < len(windows)]
    if not wanted:
        return times
    collected: dict[int, list[np.ndarray]] = {i: [] for i in wanted}
    last_end = max(windows[i][1] for i in wanted)
    for index, frame in enumerate(turns.iter_picture_frames(video_path, fps=SAMPLE_FPS)):
        at = index / SAMPLE_FPS
        if at > last_end:
            break
        for i in wanted:
            if windows[i][0] <= at < windows[i][1]:
                collected[i].append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    for i in wanted:
        # the first frame collected is the first at or after the window's start
        first = np.ceil(windows[i][0] * SAMPLE_FPS) / SAMPLE_FPS
        for action, at in _locate(collected[i], first, plays[i]).items():
            times[(i + 1, action)] = at
    return times
