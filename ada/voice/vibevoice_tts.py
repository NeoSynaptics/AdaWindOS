"""VibeVoice Realtime 0.5B TTS service for Pipecat.

Generates speech using Microsoft VibeVoice Streaming model.
Streams audio chunks as they're generated — no waiting for full sentence.

Requires: vibevoice (pip install -e .[streamingtts])
Model: microsoft/VibeVoice-Realtime-0.5B
VRAM: ~2-3GB on GPU (bfloat16)
Sample rate: 24000Hz
"""

import asyncio
import copy
import logging
import time
from typing import Optional

import numpy as np
import torch

from pipecat.frames.frames import (
    OutputAudioRawFrame,
    TextFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection

log = logging.getLogger("ada.voice.vibevoice")


class VibeVoiceTtsService(FrameProcessor):
    """Pipecat TTS processor using VibeVoice Realtime 0.5B with streaming.

    Receives TextFrame, generates audio in streaming chunks,
    pushes OutputAudioRawFrame for each chunk as it's generated.
    """

    def __init__(
        self,
        model_id: str = "microsoft/VibeVoice-Realtime-0.5B",
        voice_preset: str = "en-Emma_woman",
        device: str = "cuda:0",
        sample_rate: int = 24000,
        cfg_scale: float = 1.5,
        ddpm_steps: int = 5,
        voice_preset_path: Optional[str] = None,
    ):
        super().__init__()
        self._model_id = model_id
        self._voice_preset = voice_preset
        self._device = device
        self._sample_rate = sample_rate
        self._cfg_scale = cfg_scale
        self._ddpm_steps = ddpm_steps
        self._voice_preset_path = voice_preset_path
        self._model = None
        self._processor = None
        self._cached_prompt = None
        self._loaded = False

    async def _ensure_loaded(self):
        """Lazy-load model on first use."""
        if self._loaded:
            return

        log.info(f"Loading VibeVoice from {self._model_id}...")
        t0 = time.time()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._load_model)

        log.info(f"VibeVoice loaded in {time.time()-t0:.1f}s on {self._device}")
        self._loaded = True

    def _load_model(self):
        from vibevoice.modular.modeling_vibevoice_streaming_inference import (
            VibeVoiceStreamingForConditionalGenerationInference,
        )
        from vibevoice.processor.vibevoice_streaming_processor import (
            VibeVoiceStreamingProcessor,
        )

        self._processor = VibeVoiceStreamingProcessor.from_pretrained(self._model_id)

        try:
            self._model = VibeVoiceStreamingForConditionalGenerationInference.from_pretrained(
                self._model_id,
                torch_dtype=torch.bfloat16,
                device_map=self._device,
                attn_implementation="flash_attention_2",
            )
        except Exception as e:
            log.warning(f"Flash attention failed ({e}), falling back to SDPA")
            self._model = VibeVoiceStreamingForConditionalGenerationInference.from_pretrained(
                self._model_id,
                torch_dtype=torch.bfloat16,
                device_map=self._device,
                attn_implementation="sdpa",
            )

        self._model.eval()
        self._model.set_ddpm_inference_steps(num_steps=self._ddpm_steps)

        # Load voice preset
        voice_path = self._voice_preset_path
        if voice_path is None:
            import glob
            import os
            # Look in the VibeVoice demo voices directory
            vibevoice_dir = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)
            )))
            # Try common locations
            search_paths = [
                os.path.expanduser(f"~/GitHub/VibeVoice/demo/voices/streaming_model/{self._voice_preset}.pt"),
                os.path.join(vibevoice_dir, f"demo/voices/streaming_model/{self._voice_preset}.pt"),
            ]
            for p in search_paths:
                if os.path.exists(p):
                    voice_path = p
                    break

        if voice_path is None:
            raise FileNotFoundError(
                f"Voice preset '{self._voice_preset}' not found. "
                f"Searched: {search_paths}"
            )

        log.info(f"Loading voice preset: {voice_path}")
        self._cached_prompt = torch.load(
            voice_path, map_location=self._device, weights_only=False
        )

    def _generate_streaming(self, text: str):
        """Generate audio from text with streaming chunks.

        Returns list of audio byte chunks (int16 at 24kHz).
        Uses AudioStreamer to collect chunks as they're decoded.
        """
        from vibevoice.modular.streamer import AudioStreamer
        from queue import Empty
        import threading

        # Prepare inputs
        inputs = self._processor.process_input_with_cached_prompt(
            text=text,
            cached_prompt=self._cached_prompt,
            padding=True,
            return_tensors="pt",
            return_attention_mask=True,
        )

        # Move to device
        for k, v in inputs.items():
            if torch.is_tensor(v):
                inputs[k] = v.to(self._device)

        # Create streamer — chunks are pushed to queue during generation
        streamer = AudioStreamer(batch_size=1, stop_signal="STOP", timeout=60.0)

        audio_chunks = []
        gen_error = [None]

        def _run_generate():
            try:
                self._model.generate(
                    **inputs,
                    max_new_tokens=None,
                    cfg_scale=self._cfg_scale,
                    tokenizer=self._processor.tokenizer,
                    generation_config={"do_sample": False},
                    verbose=False,
                    audio_streamer=streamer,
                    all_prefilled_outputs=copy.deepcopy(self._cached_prompt),
                    show_progress_bar=False,
                )
            except Exception as e:
                gen_error[0] = e
                streamer.end()

        # Run generation in a thread so we can drain chunks concurrently
        gen_thread = threading.Thread(target=_run_generate)
        gen_thread.start()

        # Drain audio chunks from the streamer queue as they arrive
        queue = streamer.audio_queues[0]
        while True:
            try:
                chunk = queue.get(timeout=60.0)
            except Empty:
                break
            if chunk == "STOP":
                break
            # Convert chunk tensor to int16 bytes
            audio_np = chunk.float().cpu().numpy().squeeze()
            audio_np = (audio_np * 32767).clip(-32768, 32767).astype(np.int16)
            audio_chunks.append(audio_np.tobytes())

        gen_thread.join(timeout=10.0)

        if gen_error[0]:
            raise gen_error[0]

        return audio_chunks

    def _generate_batch(self, text: str) -> bytes:
        """Fallback: generate full audio at once (non-streaming)."""
        inputs = self._processor.process_input_with_cached_prompt(
            text=text,
            cached_prompt=self._cached_prompt,
            padding=True,
            return_tensors="pt",
            return_attention_mask=True,
        )

        for k, v in inputs.items():
            if torch.is_tensor(v):
                inputs[k] = v.to(self._device)

        outputs = self._model.generate(
            **inputs,
            max_new_tokens=None,
            cfg_scale=self._cfg_scale,
            tokenizer=self._processor.tokenizer,
            generation_config={"do_sample": False},
            verbose=False,
            all_prefilled_outputs=copy.deepcopy(self._cached_prompt),
            show_progress_bar=False,
        )

        if outputs.speech_outputs and outputs.speech_outputs[0] is not None:
            audio = outputs.speech_outputs[0]
            audio_np = audio.float().cpu().numpy().squeeze()
            audio_np = (audio_np * 32767).clip(-32768, 32767).astype(np.int16)
            return audio_np.tobytes()

        return b""

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TextFrame):
            text = frame.text.strip()
            if not text:
                await self.push_frame(frame, direction)
                return

            await self._ensure_loaded()

            log.info(f"[VibeVoice TTS] Generating: {text[:60]}")
            t0 = time.time()

            # Run generation in executor (streaming collects chunks)
            loop = asyncio.get_event_loop()
            try:
                audio_chunks = await loop.run_in_executor(
                    None, self._generate_streaming, text
                )
            except Exception as e:
                log.error(f"Streaming generation failed: {e}, trying batch")
                audio_bytes = await loop.run_in_executor(
                    None, self._generate_batch, text
                )
                audio_chunks = [audio_bytes] if audio_bytes else []

            gen_time = time.time() - t0
            total_bytes = sum(len(c) for c in audio_chunks)
            dur = total_bytes / 2 / self._sample_rate
            log.info(
                f"[VibeVoice TTS] Generated {dur:.1f}s audio in {gen_time:.1f}s "
                f"({len(audio_chunks)} chunks)"
            )

            # Push each chunk as a separate audio frame for streaming playback
            for chunk in audio_chunks:
                if chunk:
                    await self.push_frame(
                        OutputAudioRawFrame(
                            audio=chunk,
                            sample_rate=self._sample_rate,
                            num_channels=1,
                        )
                    )

        elif isinstance(frame, (LLMFullResponseStartFrame, LLMFullResponseEndFrame)):
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)
