"""Turn spoken notes into text, on this machine.

Uses faster-whisper, an optional dependency (`pip install -e ".[transcribe]"`).
The model is downloaded once on first use (the "small" one is a few hundred
MB) and nothing is sent anywhere after that.
"""

from __future__ import annotations

import importlib.util
import subprocess
import threading
from pathlib import Path

import numpy as np

from .recorder import require_ffmpeg

# "small" is the smallest model that handles Dutch with English card names
# mixed in reasonably; "base" is faster but noticeably worse at that.
MODEL_NAME = "small"

_model = None
_model_lock = threading.Lock()


def available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def _load_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
        return _model


def _decode(audio_path: Path) -> np.ndarray:
    """The audio as 16 kHz mono floats, which is what the model takes.

    Decoded with ffmpeg rather than left to faster-whisper: its own decoder
    broke against the installed PyAV (an argument PyAV no longer accepts),
    and ffmpeg is needed for recording anyway.
    """
    result = subprocess.run(
        [
            require_ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-i", str(audio_path),
            "-ac", "1", "-ar", "16000", "-f", "f32le", "-",
        ],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(result.stdout, dtype=np.float32)


def transcribe(audio_path: Path, vocabulary: list[str] | None = None) -> str:
    """The words spoken in `audio_path`. `vocabulary` (card and deck names)
    is given to the model as context so it spells those the way you do."""
    prompt = None
    if vocabulary:
        prompt = "Pokémon TCG: " + ", ".join(vocabulary[:60])
    segments, _ = _load_model().transcribe(
        _decode(audio_path),
        initial_prompt=prompt,
        vad_filter=True,  # skip silence instead of inventing words for it
    )
    return " ".join(segment.text.strip() for segment in segments).strip()
