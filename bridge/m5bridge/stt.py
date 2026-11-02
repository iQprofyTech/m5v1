"""Speech-to-text with faster-whisper (runs locally, model is downloaded on first use)."""

from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np

from .config import Config

log = logging.getLogger(__name__)

WHISPER_RATE = 16000


def pcm16_to_float(pcm: bytes, sample_rate: int) -> np.ndarray:
    audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    if sample_rate != WHISPER_RATE and audio.size:
        n_out = int(audio.size * WHISPER_RATE / sample_rate)
        audio = np.interp(
            np.linspace(0, audio.size - 1, n_out), np.arange(audio.size), audio
        ).astype(np.float32)
    # The Cardputer mic is quiet; normalise so the VAD does not discard speech.
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if 0.0 < peak < 0.5:
        audio *= min(0.9 / peak, 20.0)
    return audio


class Transcriber:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None
        self._load_lock = threading.Lock()

    def _get_model(self):
        with self._load_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                log.info("loading whisper model %s (%s)", self.cfg.whisper_model, self.cfg.whisper_device)
                self._model = WhisperModel(
                    self.cfg.whisper_model,
                    device=self.cfg.whisper_device,
                    compute_type=self.cfg.whisper_compute,
                )
            return self._model

    def preload(self) -> None:
        self._get_model()

    def _transcribe_sync(self, pcm: bytes, sample_rate: int) -> str:
        audio = pcm16_to_float(pcm, sample_rate)
        if audio.size < WHISPER_RATE // 4:
            return ""
        segments, _info = self._get_model().transcribe(
            audio,
            language=self.cfg.language or None,
            beam_size=5,
            vad_filter=True,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return await asyncio.to_thread(self._transcribe_sync, pcm, sample_rate)
