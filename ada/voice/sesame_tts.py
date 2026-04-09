"""Sesame CSM TTS service for Pipecat.

Generates speech using Sesame CSM-1B via HuggingFace Transformers.
Uses a fixed speaker ID for consistent voice across all turns.

Requires: transformers>=4.52, accelerate, soundfile
Model: sesame/csm-1b (gated, requires HF login)
VRAM: ~4-6GB on GPU (fp16)
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import numpy as np
import torch

from pipecat.frames.frames import (
    OutputAudioRawFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TextFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

if TYPE_CHECKING:
    pass

log = logging.getLogger("ada.voice.sesame")



class SesameTtsService(FrameProcessor):
    """Pipecat TTS processor using Sesame CSM-1B.

    Receives TextFrame, generates audio, pushes TTSAudioRawFrame.
    Uses speaker ID 0 for consistent Ada voice.
    """

    def __init__(
        self,
        model_id: str = "sesame/csm-1b",
        speaker_id: int = 0,
        device: str = "cuda:0",
        sample_rate: int = 24000,
    ):
        super().__init__()
        self._model_id = model_id
        self._speaker_id = speaker_id
        self._device = device
        self._sample_rate = sample_rate
        self._model = None
        self._processor = None
        self._loaded = False

    async def _ensure_loaded(self):
        """Lazy-load model on first use."""
        if self._loaded:
            return

        log.info(f"Loading Sesame CSM from {self._model_id}...")
        t0 = time.time()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._load_model)

        log.info(f"Sesame CSM loaded in {time.time()-t0:.1f}s on {self._device}")
        self._loaded = True

    def _load_model(self):
        from transformers import CsmForConditionalGeneration, AutoProcessor

        self._processor = AutoProcessor.from_pretrained(self._model_id)
        self._model = CsmForConditionalGeneration.from_pretrained(
            self._model_id,
            device_map=self._device,
            dtype=torch.float16,
        )

    def _generate_sync(self, text: str) -> bytes:
        """Generate audio from text (sync, runs in executor)."""
        tagged = f"[{self._speaker_id}]{text}"
        inputs = self._processor(tagged, add_special_tokens=True).to(self._device)

        with torch.no_grad():
            audio_out = self._model.generate(**inputs, output_audio=True)

        # Output is a list of tensors — take the first one
        if isinstance(audio_out, list):
            audio = audio_out[0]
        else:
            audio = audio_out

        # Convert to int16 bytes — must cast to float32 first (float16 overflows)
        audio_np = audio.float().cpu().numpy().squeeze()
        audio_np = (audio_np * 32767).clip(-32768, 32767).astype(np.int16)

        return audio_np.tobytes()

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TextFrame):
            text = frame.text.strip()
            if not text:
                await self.push_frame(frame, direction)
                return

            await self._ensure_loaded()

            log.info(f"[CSM TTS] Generating: {text[:60]}")
            t0 = time.time()

            # Run generation in executor to avoid blocking event loop
            loop = asyncio.get_event_loop()
            audio_bytes = await loop.run_in_executor(None, self._generate_sync, text)

            gen_time = time.time() - t0
            dur = len(audio_bytes) / 2 / self._sample_rate  # int16 = 2 bytes per sample
            log.info(f"[CSM TTS] Generated {dur:.1f}s audio in {gen_time:.1f}s")

            # Push audio frame (OutputAudioRawFrame bypasses destination routing)
            await self.push_frame(
                OutputAudioRawFrame(
                    audio=audio_bytes,
                    sample_rate=self._sample_rate,
                    num_channels=1,
                )
            )

        elif isinstance(frame, (LLMFullResponseStartFrame, LLMFullResponseEndFrame)):
            # Pass through control frames
            await self.push_frame(frame, direction)

        else:
            await self.push_frame(frame, direction)
