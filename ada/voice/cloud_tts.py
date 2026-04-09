"""Cloud TTS service — Microsoft Azure Speech API for Pipecat.

Uses Azure Cognitive Services Speech SDK for high-quality neural TTS.
Streams audio chunks as they're synthesized — low latency, no GPU needed.

Pricing: ~$1 per 1M characters (neural voices)
Docs: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/

Setup:
  1. Create Azure Speech resource at portal.azure.com
  2. Set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION in .env

TODO: Implement actual Azure SDK integration. Currently stubbed.
"""

import asyncio
import logging
import os
from typing import Optional

import numpy as np

from pipecat.frames.frames import (
    OutputAudioRawFrame,
    TextFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

log = logging.getLogger("ada.voice.cloud_tts")


class CloudTtsService(FrameProcessor):
    """Pipecat TTS processor using Microsoft Azure Speech API.

    Receives TextFrame, calls Azure TTS, pushes OutputAudioRawFrame.

    TODO: Replace stub with actual azure-cognitiveservices-speech SDK calls.
    Currently generates silence so the pipeline doesn't crash.
    """

    def __init__(
        self,
        speech_key: str = "",
        speech_region: str = "eastus",
        voice: str = "en-US-AvaMultilingualNeural",
        sample_rate: int = 24000,
    ):
        super().__init__()
        self._speech_key = speech_key or os.environ.get("AZURE_SPEECH_KEY", "")
        self._speech_region = speech_region
        self._voice = voice
        self._sample_rate = sample_rate
        self._synthesizer = None
        self._initialized = False

    async def _ensure_initialized(self):
        """Lazy-init the Azure Speech SDK synthesizer."""
        if self._initialized:
            return

        if not self._speech_key:
            log.warning(
                "AZURE_SPEECH_KEY not set — cloud TTS will produce silence. "
                "Set it in .env or pass speech_key to CloudTtsService."
            )
            self._initialized = True
            return

        try:
            # TODO: Initialize azure.cognitiveservices.speech SDK here
            # import azure.cognitiveservices.speech as speechsdk
            # speech_config = speechsdk.SpeechConfig(
            #     subscription=self._speech_key,
            #     region=self._speech_region,
            # )
            # speech_config.set_speech_synthesis_output_format(
            #     speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm
            # )
            # speech_config.speech_synthesis_voice_name = self._voice
            # self._synthesizer = speechsdk.SpeechSynthesizer(
            #     speech_config=speech_config, audio_config=None
            # )
            log.info(f"Azure TTS stub initialized (voice={self._voice}, region={self._speech_region})")
            self._initialized = True
        except Exception as e:
            log.error(f"Azure TTS init failed: {e}")
            self._initialized = True

    async def _synthesize(self, text: str) -> bytes:
        """Synthesize text to raw audio bytes via Azure.

        TODO: Replace with actual Azure SDK call:
            result = self._synthesizer.speak_text_async(text).get()
            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                return result.audio_data
        """
        if not self._speech_key:
            # No API key — return short silence
            duration_samples = int(self._sample_rate * 0.5)
            return np.zeros(duration_samples, dtype=np.int16).tobytes()

        # TODO: Actual Azure API call goes here
        # For now, return silence as placeholder
        log.warning("Azure TTS stub — returning silence. Implement SDK call.")
        duration_samples = int(self._sample_rate * 0.5)
        return np.zeros(duration_samples, dtype=np.int16).tobytes()

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TextFrame):
            text = frame.text.strip()
            if not text:
                await self.push_frame(frame, direction)
                return

            await self._ensure_initialized()

            log.info(f"[Cloud TTS] Generating: {text[:60]}")

            loop = asyncio.get_event_loop()
            audio_bytes = await loop.run_in_executor(None, lambda: asyncio.run(self._synthesize(text)))

            if audio_bytes:
                await self.push_frame(
                    OutputAudioRawFrame(
                        audio=audio_bytes,
                        sample_rate=self._sample_rate,
                        num_channels=1,
                    )
                )

        elif isinstance(frame, (LLMFullResponseStartFrame, LLMFullResponseEndFrame)):
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)
