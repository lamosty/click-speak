from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


def _query_native_rate(device: int | str | None) -> float:
    """Get the device's default sample rate so we don't force CoreAudio to resample."""
    try:
        dev_idx = device if device is not None else sd.default.device[0]
        info = sd.query_devices(dev_idx, "input")
        return float(info["default_samplerate"])
    except Exception:
        return 16000.0


def _resample(audio: np.ndarray, orig_rate: float, target_rate: int) -> np.ndarray:
    """Resample audio via linear interpolation (fast, good enough for speech)."""
    if orig_rate == target_rate:
        return audio
    target_len = int(len(audio) * target_rate / orig_rate)
    if target_len == 0:
        return np.array([], dtype=np.float32)
    indices = np.linspace(0, len(audio) - 1, target_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


class AudioCapture:
    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_size: int = 512,
        energy_threshold: float = 0.01,
        device: int | str | None = None,
    ):
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.energy_threshold = energy_threshold
        self.device = device
        self.is_silent = True
        self.on_chunk: Callable[[np.ndarray], None] | None = None
        self.last_rms = 0.0
        self.last_peak = 0.0
        self.last_callback_time: float = 0.0  # monotonic timestamp of last audio callback
        self.last_nonzero_time: float = 0.0   # last time peak > 0 (real audio, not dead BT)

        self._lock = threading.Lock()
        self._recording = False
        self._recording_buffer: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None

        # Native rate + blocksize calculated to produce ~same duration chunks
        self._native_rate: float = 0.0
        self._native_blocksize: int = 0
        self._update_native_rate()

    def _update_native_rate(self) -> None:
        self._native_rate = _query_native_rate(self.device)
        chunk_duration = self.chunk_size / self.sample_rate
        self._native_blocksize = max(1, int(chunk_duration * self._native_rate))
        logger.info(
            "Audio: native_rate=%.0f target_rate=%d native_blocksize=%d",
            self._native_rate, self.sample_rate, self._native_blocksize,
        )

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        now = time.monotonic()
        self.last_callback_time = now

        if status:
            logger.warning("Audio callback status: %s", status)

        raw = indata[:, 0].copy()

        # Resample from native rate to 16kHz for ML pipeline
        chunk = _resample(raw, self._native_rate, self.sample_rate)

        rms = np.sqrt(np.mean(chunk**2))
        self.is_silent = rms < self.energy_threshold
        self.last_rms = float(rms)
        peak = float(np.max(np.abs(chunk)))
        self.last_peak = peak
        if peak > 0.0:
            self.last_nonzero_time = now

        if self._recording:
            with self._lock:
                self._recording_buffer.append(chunk)

        if self.on_chunk is not None:
            self.on_chunk(chunk)

    def start(self) -> None:
        if self._stream is not None:
            return
        self._update_native_rate()
        self._stream = sd.InputStream(
            samplerate=self._native_rate,
            channels=1,
            dtype="float32",
            blocksize=self._native_blocksize,
            device=self.device,
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.info("Audio stream started at %.0f Hz (resampling to %d Hz)", self._native_rate, self.sample_rate)

    def stop(self) -> None:
        if self._stream is None:
            return
        self._stream.stop()
        self._stream.close()
        self._stream = None

    def start_recording(self) -> None:
        with self._lock:
            self._recording_buffer.clear()
        self._recording = True

    def stop_recording(self) -> np.ndarray:
        self._recording = False
        with self._lock:
            if not self._recording_buffer:
                return np.array([], dtype=np.float32)
            audio = np.concatenate(self._recording_buffer)
            self._recording_buffer.clear()
        return audio

    def set_energy_threshold(self, energy_threshold: float) -> None:
        self.energy_threshold = float(energy_threshold)

    def measure_level(self, duration_seconds: float = 1.0) -> float:
        if self._stream is not None:
            return 0.0

        native_rate = _query_native_rate(self.device)
        levels: list[float] = []
        event = threading.Event()

        def callback(
            indata: np.ndarray,
            frames: int,
            time_info: object,
            status: sd.CallbackFlags,
        ) -> None:
            if status:
                logger.warning("Test capture status: %s", status)
            chunk = indata[:, 0].copy()
            rms = float(np.sqrt(np.mean(chunk**2)))
            levels.append(rms)

        stream = sd.InputStream(
            samplerate=native_rate,
            channels=1,
            dtype="float32",
            blocksize=self._native_blocksize,
            device=self.device,
            callback=callback,
        )

        try:
            stream.start()
            event.wait(duration_seconds)
            if levels:
                return max(levels)
        finally:
            stream.stop()
            stream.close()

        return 0.0

    def set_device(self, device: int | str | None) -> None:
        was_running = self._stream is not None
        if was_running:
            self.stop()
        self.device = device
        self._update_native_rate()
        if was_running:
            self.start()


def refresh_devices() -> None:
    """Reinitialize PortAudio to get a fresh device list.

    PortAudio caches devices at init time. Bluetooth devices that
    connect/disconnect after init won't be seen without this."""
    try:
        sd._terminate()
        sd._initialize()
        logger.info("PortAudio reinitialized — device list refreshed")
    except Exception:
        logger.exception("Failed to reinitialize PortAudio")


def list_input_devices() -> list[dict]:
    results = []
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            results.append({"index": i, "name": dev["name"]})
    return results


def get_device_by_name(name: str) -> int | None:
    for i, dev in enumerate(sd.query_devices()):
        if name.lower() in dev["name"].lower():
            return i
    return None
