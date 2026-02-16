from __future__ import annotations

import logging
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

MODEL_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
MODEL_DIR = Path.home() / ".config" / "clickspeak"
MODEL_PATH = MODEL_DIR / "silero_vad.onnx"
_MIN_MODEL_SIZE = 1_000_000  # sanity check: model should be >1MB


class VoiceActivityDetector:
    def __init__(
        self,
        silence_threshold: float = 1.5,
        sample_rate: int = 16000,
        speech_threshold: float = 0.5,
    ) -> None:
        self.silence_threshold = silence_threshold
        self.sample_rate = sample_rate
        self.speech_threshold = speech_threshold
        self.on_speech_end: Callable[[], None] | None = None
        self.enabled: bool = True

        self._session = None
        self._state: np.ndarray | None = None
        self._sr: np.ndarray | None = None
        self._context: np.ndarray | None = None
        self._context_size = 64 if sample_rate == 16000 else 32

        self._speech_detected: bool = False
        self._silence_start: float | None = None
        self.last_speech_prob: float = 0.0

    def _ensure_model(self) -> None:
        if self._session is not None:
            return

        if not MODEL_PATH.exists() or MODEL_PATH.stat().st_size < _MIN_MODEL_SIZE:
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            logger.info("Downloading Silero VAD model to %s", MODEL_PATH)
            fd, tmp = tempfile.mkstemp(dir=MODEL_DIR, suffix=".onnx.tmp")
            try:
                import os
                os.close(fd)
                urllib.request.urlretrieve(MODEL_URL, tmp)
                if Path(tmp).stat().st_size < _MIN_MODEL_SIZE:
                    raise RuntimeError("Downloaded VAD model is too small — likely corrupt")
                Path(tmp).replace(MODEL_PATH)
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise

        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1

        self._session = ort.InferenceSession(str(MODEL_PATH), sess_options=opts)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self._context_size), dtype=np.float32)
        self._sr = np.array(self.sample_rate, dtype=np.int64)
        logger.info("Loaded Silero VAD model")

    def process(self, audio_chunk: np.ndarray) -> bool:
        if not self.enabled:
            return False

        self._ensure_model()

        audio = audio_chunk.astype(np.float32).reshape(1, -1)
        audio_with_context = np.concatenate([self._context, audio], axis=1)
        out, self._state = self._session.run(
            None,
            {"input": audio_with_context, "state": self._state, "sr": self._sr},
        )
        self._context = audio_with_context[:, -self._context_size:]
        speech_prob = out[0][0]
        self.last_speech_prob = float(speech_prob)

        if speech_prob >= self.speech_threshold:
            self._speech_detected = True
            self._silence_start = None
            return False

        if self._speech_detected:
            now = time.monotonic()

            if self._silence_start is None:
                self._silence_start = now

            elapsed_silence = now - self._silence_start

            if elapsed_silence >= self.silence_threshold:
                logger.info(
                    "End of speech detected (%.1fs silence)", elapsed_silence
                )
                if self.on_speech_end is not None:
                    self.on_speech_end()
                self._speech_detected = False
                self._silence_start = None
                return True

        return False

    def reset(self) -> None:
        self._speech_detected = False
        self._silence_start = None
        if self._session is not None:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
            self._context = np.zeros((1, self._context_size), dtype=np.float32)

    def set_silence_threshold(self, silence_threshold: float) -> None:
        self.silence_threshold = float(silence_threshold)
        self.reset()
