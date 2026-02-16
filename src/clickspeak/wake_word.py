from __future__ import annotations

import logging
import time
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)


_WAKEWORD_CHUNK_SIZE = 1280  # openwakeword expects 1280 samples (80ms at 16kHz)


class WakeWordDetector:
    def __init__(self, model_name: str = "hey_jarvis", threshold: float = 0.5) -> None:
        self.model_name = model_name
        self.threshold = threshold
        self.on_detected: Callable[[], None] | None = None
        self.enabled: bool = True
        self._model = None
        self.last_score: float = 0.0
        self._disable_on_error: bool = False
        self._buffer = np.array([], dtype=np.float32)
        self._cooldown_until: float = 0.0
        self._cooldown_seconds: float = 2.0

    def _normalize_model_name(self, model_name: str) -> str:
        return model_name.strip().replace(" ", "_").replace("-", "_").lower()

    def _ensure_model(self) -> None:
        if self._disable_on_error:
            return

        if self._model is not None:
            return

        from openwakeword.model import Model
        import openwakeword

        try:
            self.model_name = self._normalize_model_name(self.model_name)
            self._model = Model()
            if self.model_name not in self._model.models:
                available = ", ".join(sorted(self._model.models.keys()))
                raise ValueError(
                    f"Unknown wake word model '{self.model_name}'. Available: {available}"
                )
            logger.info("Loaded wake word model: %s", self.model_name)
        except Exception as exc:
            self._disable_on_error = True
            self._model = None
            self.enabled = False
            logger.error("Disabling wake-word; failed to initialize model '%s': %s", self.model_name, exc)

    def set_cooldown(self, seconds: float) -> None:
        self._cooldown_until = time.monotonic() + seconds

    def process(self, audio_chunk: np.ndarray) -> bool:
        if not self.enabled:
            return False

        if self._disable_on_error:
            return False

        self._ensure_model()
        if self._disable_on_error or self._model is None:
            return False

        # Buffer incoming chunks until we have enough for openwakeword
        self._buffer = np.concatenate([self._buffer, audio_chunk.astype(np.float32)])

        detected = False
        while len(self._buffer) >= _WAKEWORD_CHUNK_SIZE:
            chunk = self._buffer[:_WAKEWORD_CHUNK_SIZE]
            self._buffer = self._buffer[_WAKEWORD_CHUNK_SIZE:]

            audio_int16 = (chunk * 32767).astype(np.int16)
            prediction = self._model.predict(audio_int16)
            score = prediction.get(self.model_name, 0.0)
            self.last_score = float(score)

            if score >= self.threshold:
                now = time.monotonic()
                if now < self._cooldown_until:
                    continue
                self._cooldown_until = now + self._cooldown_seconds
                logger.info("Wake word detected (score=%.3f)", score)
                if self.on_detected is not None:
                    self.on_detected()
                detected = True
                break

        return detected

    def reset(self) -> None:
        self._buffer = np.array([], dtype=np.float32)
        if self._model is not None:
            self._model.reset()

    def set_threshold(self, threshold: float) -> None:
        self.threshold = float(threshold)

    def set_model(self, model_name: str) -> None:
        self.model_name = self._normalize_model_name(model_name)
        self._model = None
        self._disable_on_error = False
