import numpy as np

from pokemontcg_analyser import turns


def _frame(opponent: int, you: int) -> tuple[np.ndarray, np.ndarray]:
    """A picture whose upper band is the opponent's ring half and whose
    lower band is mine, at the given brightness."""
    mask = np.zeros((turns.HEIGHT, turns.WIDTH), dtype=np.uint8)
    mask[10:20] = turns.MASK_OPPONENT
    mask[200:210] = turns.MASK_YOU
    frame = np.full((turns.HEIGHT, turns.WIDTH, 3), 60, dtype=np.uint8)
    frame[10:20] = opponent
    frame[200:210] = you
    return frame, mask


def test_ring_state_reads_the_brighter_half() -> None:
    assert turns.ring_state(*_frame(215, 85)) == "opponent"
    assert turns.ring_state(*_frame(85, 215)) == "you"
    # slightly misaligned picture: weaker contrast, same answer
    assert turns.ring_state(*_frame(150, 80)) == "opponent"


def test_ring_state_is_unread_when_a_dialog_darkens_the_board() -> None:
    assert turns.ring_state(*_frame(30, 30)) is None
    assert turns.ring_state(*_frame(90, 85)) is None


def test_turns_from_states_needs_a_change_to_persist() -> None:
    o, y, n = "opponent", "you", None
    states = [n] * 8 + [o] * 12 + [y] * 2 + [o] * 6 + [n] * 4 + [o] * 4 + [y] * 10

    found = turns.turns_from_states(states, fps=4)

    # the 0.5s flash of "you" is ignored, and the dark gap doesn't end the turn
    assert found == [turns.Turn(2.0, "opponent"), turns.Turn(9.0, "you")]


def test_ring_mask_has_both_halves() -> None:
    import cv2

    mask = cv2.imread(str(turns.RING_MASK_PATH), cv2.IMREAD_GRAYSCALE)

    assert mask.shape == (turns.HEIGHT, turns.WIDTH)
    assert (mask == turns.MASK_OPPONENT).sum() > 300
    assert (mask == turns.MASK_YOU).sum() > 300


def _game(zoom: float, dx: int, dy: int, frames: int = 40) -> list[np.ndarray]:
    """Frames of a game whose ring is shown `zoom` times the size the mask
    was measured at and shifted: halves lit in turn, a few seconds each."""
    import cv2

    mask = cv2.imread(str(turns.RING_MASK_PATH), cv2.IMREAD_GRAYSCALE)
    matrix = np.float32(
        [
            [zoom, 0, turns.WIDTH / 2 * (1 - zoom) + dx],
            [0, zoom, turns.HEIGHT / 2 * (1 - zoom) + dy],
        ]
    )
    shown = cv2.warpAffine(mask, matrix, (turns.WIDTH, turns.HEIGHT), flags=cv2.INTER_NEAREST)
    rng = np.random.default_rng(1)
    made = []
    for index in range(frames):
        frame = rng.integers(60, 110, size=(turns.HEIGHT, turns.WIDTH, 3), dtype=np.uint8)
        opponents_turn = (index // 5) % 2 == 0
        frame[shown == turns.MASK_OPPONENT] = 215 if opponents_turn else 85
        frame[shown == turns.MASK_YOU] = 85 if opponents_turn else 215
        made.append(frame)
    return made


def test_find_layout_finds_a_ring_that_is_larger_and_moved() -> None:
    found = turns.find_layout(_game(zoom=1.09, dx=0, dy=5))

    assert abs(found.zoom - 1.09) <= 0.02
    assert abs(found.dx) <= 2 and abs(found.dy - 5) <= 2


def test_find_layout_leaves_a_recording_cut_to_the_game_alone() -> None:
    found = turns.find_layout(_game(zoom=1.0, dx=0, dy=0))

    assert abs(found.zoom - 1.0) <= 0.01 and abs(found.dx) <= 1 and abs(found.dy) <= 1


def test_layout_brings_frames_back_to_the_picture_the_mask_fits() -> None:
    import cv2

    frames = _game(zoom=1.12, dx=-20, dy=8)
    found = turns.find_layout(frames)
    mask = cv2.imread(str(turns.RING_MASK_PATH), cv2.IMREAD_GRAYSCALE)

    states = [
        turns.ring_state(cv2.warpAffine(f, found.matrix(), (turns.WIDTH, turns.HEIGHT)), mask)
        for f in frames
    ]

    assert states[:5] == ["opponent"] * 5 and states[5:10] == ["you"] * 5
    # without it the mask misses the ring altogether
    assert turns.ring_state(frames[0], mask) is None
