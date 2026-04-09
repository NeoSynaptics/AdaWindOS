"""Voice layer — Pipecat pipeline with VAD, STT, TTS, and wake word."""

from .pipeline import VoicePipeline
from .wakeword import WakeWordGate

__all__ = ["VoicePipeline", "WakeWordGate"]
