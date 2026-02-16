from __future__ import annotations

import builtins
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from parakeet_mlx import from_pretrained

DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"

# parakeet_mlx opens JSON files with open(..., "r") which defaults to
# ASCII encoding inside .app bundles. Temporarily patch builtins.open
# to force UTF-8 for text-mode opens during model loading.
_real_open = builtins.open


def _utf8_open(*args, **kwargs):
    if len(args) >= 2 and "b" not in str(args[1]):
        kwargs.setdefault("encoding", "utf-8")
    elif not args or (len(args) < 2 and "b" not in kwargs.get("mode", "r")):
        kwargs.setdefault("encoding", "utf-8")
    return _real_open(*args, **kwargs)


class Transcriber:
    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model = None

    def _load_model(self) -> None:
        builtins.open = _utf8_open
        try:
            self._model = from_pretrained(self._model_name)
        finally:
            builtins.open = _real_open

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> str:
        if self._model is None:
            self._load_model()

        audio = audio.astype(np.float32)

        # parakeet_mlx.transcribe() expects a file path, not a numpy array
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = Path(f.name)

        try:
            sf.write(str(tmp_path), audio, sample_rate)
            builtins.open = _utf8_open
            try:
                result = self._model.transcribe(tmp_path)
            finally:
                builtins.open = _real_open
            return result.text
        finally:
            tmp_path.unlink(missing_ok=True)
