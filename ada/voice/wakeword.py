"""Wake word detection using OpenWakeWord.

When Ada is in IDLE state, the wake word detector listens for "hey ada".
Once detected, the voice state transitions to ATTENTIVE and the full
STT pipeline activates. After attentive_timeout_sec of silence, Ada
returns to IDLE and the wake word detector reactivates.
"""

import asyncio
import logging
import numpy as np
from typing import TYPE_CHECKING

from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    InputAudioRawFrame,
    UserStartedSpeakingFrame,
)

if TYPE_CHECKING:
    from ada.state import SystemState
    from ada.config import VoiceConfig

log = logging.getLogger("ada.voice.wakeword")


class WakeWordGate(FrameProcessor):
    """Gates audio frames based on wake word detection.

    In IDLE state: listens for wake word, blocks STT frames.
    In ATTENTIVE/LISTENING state: passes all frames through.
    After timeout: returns to IDLE.
    """

    def __init__(self, state: "SystemState", config: "VoiceConfig"):
        super().__init__()
        self._state = state
        self._config = config
        self._detector = None
        self._attentive_task: asyncio.Task | None = None
        self._always_listen = False  # set True to skip wake word

    async def _ensure_detector(self):
        """Lazy-load OpenWakeWord model."""
        if self._detector is None:
            try:
                from openwakeword.model import Model
                self._detector = Model(
                    wakeword_models=["hey_jarvis"],  # closest built-in; custom "hey ada" can be trained
                    inference_framework="onnx",
                )
                log.info("Wake word detector loaded (using 'hey_jarvis' as proxy for 'hey ada')")
            except Exception as e:
                log.warning(f"Wake word detection unavailable: {e}. Running in always-listen mode.")
                self._always_listen = True

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        from ada.state import VoiceState

        # Always pass non-audio frames through
        if not isinstance(frame, InputAudioRawFrame):
            await self.push_frame(frame, direction)
            return

        # If always-listen mode or already attentive/listening, pass through
        if self._always_listen or self._state.voice != VoiceState.IDLE:
            self._reset_attentive_timer()
            await self.push_frame(frame, direction)
            return

        # IDLE state: check for wake word
        await self._ensure_detector()
        if self._detector is None:
            await self.push_frame(frame, direction)
            return

        # Feed audio to wake word detector
        audio_data = np.frombuffer(frame.audio, dtype=np.int16)
        prediction = self._detector.predict(audio_data)

        # Check if any wake word model triggered
        for model_name, score in prediction.items():
            if score > 0.5:
                log.info(f"Wake word detected! (model={model_name}, score={score:.2f})")
                try:
                    self._state.transition_voice(VoiceState.ATTENTIVE)
                except Exception:
                    self._state.voice = VoiceState.ATTENTIVE
                self._reset_attentive_timer()
                # Pass through the frame that triggered wake word
                await self.push_frame(frame, direction)
                return

        # Wake word not detected, swallow the audio frame
        return

    def _reset_attentive_timer(self):
        """Reset the timeout that returns Ada to IDLE."""
        if self._attentive_task and not self._attentive_task.done():
            self._attentive_task.cancel()
        self._attentive_task = asyncio.create_task(self._attentive_timeout())

    async def _attentive_timeout(self):
        """After timeout seconds of no voice activity, return to IDLE."""
        try:
            await asyncio.sleep(self._config.attentive_timeout_sec)
            from ada.state import VoiceState
            if self._state.voice == VoiceState.ATTENTIVE:
                log.info("Attentive timeout — returning to IDLE")
                self._state.voice = VoiceState.IDLE
        except asyncio.CancelledError:
            pass
