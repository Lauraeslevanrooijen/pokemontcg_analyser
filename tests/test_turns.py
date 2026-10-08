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
