"""Voice pipeline — Pipecat-based local audio with Silero VAD, faster-whisper STT, VibeVoice TTS.

Architecture:
  Mic → WakeWordGate → Silero VAD → faster-whisper STT → AdaBridge → VibeVoice TTS → Speaker

The pipeline runs Pipecat's local audio transport with VAD-gated speech detection.
WakeWordGate filters audio in IDLE state (only passes through after wake word).
When the user finishes speaking, the transcript is sent to Ada's decision engine.
Ada's response is spoken back through Kokoro TTS.

Barge-in: if the user starts speaking while Ada is talking, TTS is cancelled
and the new utterance is processed.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.services.whisper.stt import WhisperSTTService, WhisperSTTSettings
from pipecat.services.kokoro.tts import KokoroTTSService, KokoroTTSSettings
from .vibevoice_tts import VibeVoiceTtsService
from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
    TextFrame,
    TTSTextFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    LLMTextFrame,
    LLMFullResponseStartFrame,
    LLMFullResponseEndFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.processors.audio.vad_processor import VADProcessor

from .wakeword import WakeWordGate

if TYPE_CHECKING:
    from ada.config import AdaConfig, VoiceConfig
    from ada.state import SystemState

log = logging.getLogger("ada.voice")


class InputResampler(FrameProcessor):
    """Resamples input audio from hardware rate (e.g. 44100Hz) to pipeline rate (16000Hz).

    Required because many USB/built-in audio devices only support 44100/48000Hz,
    but Silero VAD and Whisper STT expect 16000Hz audio.
    Output audio (TTS → speaker) is resampled by Pipecat's base_output transport.

    Uses resampy (kaiser_fast) for direct chunk-by-chunk resampling — no buffering,
    every input chunk produces an output chunk immediately.
    """

    def __init__(self, from_rate: int, to_rate: int):
        super().__init__()
        self._from_rate = from_rate
        self._to_rate = to_rate
        self._skip = (from_rate == to_rate)
        self._frame_count = 0
        # Pre-compute ratio for scipy resample_poly (faster than resampy)
        from math import gcd
        g = gcd(to_rate, from_rate)
        self._up = to_rate // g
        self._down = from_rate // g
        log.info(f"Input resampler: {from_rate}Hz → {to_rate}Hz (ratio {self._up}/{self._down})"
                 + (" (passthrough)" if self._skip else ""))

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame) and not self._skip:
            import numpy as np
            from scipy.signal import resample_poly

            samples = np.frombuffer(frame.audio, dtype=np.int16)
            resampled = resample_poly(samples, self._up, self._down).astype(np.int16)
            audio_bytes = resampled.tobytes()

            self._frame_count += 1
            if self._frame_count <= 3 or self._frame_count % 500 == 0:
                energy = np.abs(samples).mean()
                log.info(f"Resampler frame #{self._frame_count}: {len(frame.audio)}→{len(audio_bytes)} bytes, energy={energy:.0f}")

            await self.push_frame(
                InputAudioRawFrame(
                    audio=audio_bytes,
                    sample_rate=self._to_rate,
                    num_channels=frame.num_channels,
                ),
                direction,
            )
        else:
            await self.push_frame(frame, direction)


class AdaBridge(FrameProcessor):
    """Bridges Pipecat STT output to Ada's decision engine and back to TTS.

    Receives TranscriptionFrame from whisper. Uses streaming response:
    - Classification + action: fast (~100ms)
    - Response: streams sentence-by-sentence → each sentence pushed to TTS immediately
    - First audio reaches the speaker ~1-2s after STT completes
    """

    def __init__(self, ada_instance, state: "SystemState"):
        super().__init__()
        self._ada = ada_instance
        self._state = state
        self._processing = False
        self._first_sentence = True
        self._response_sentences: list[str] = []

    async def _push_sentence(self, sentence: str):
        """Callback for streaming — pushes one sentence to TTS and UI."""
        from ada.state import VoiceState

        # Signal start of LLM response on first sentence (TTS needs this to flush)
        if self._first_sentence:
            self._first_sentence = False
            await self.push_frame(LLMFullResponseStartFrame())
            if self._state.voice != VoiceState.SPEAKING:
                try:
                    self._state.transition_voice(VoiceState.SPEAKING)
                except Exception:
                    self._state.voice = VoiceState.SPEAKING

        self._response_sentences.append(sentence)
        await self.push_frame(TextFrame(text=sentence))

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = frame.text.strip()
            if not text or self._processing:
                return

            log.info(f"[STT] {text}")
            self._processing = True
            self._first_sentence = True
            self._response_sentences = []

            # Push transcript to UI so voice input appears in the chat
            try:
                from ui.server import broadcast_transcript
                asyncio.create_task(broadcast_transcript(text, "voice"))
            except ImportError:
                pass

            try:
                from ada.state import VoiceState
                try:
                    self._state.transition_voice(VoiceState.PROCESSING_FAST)
                except Exception:
                    self._state.voice = VoiceState.PROCESSING_FAST

                # Streaming: classify + act + stream response sentence-by-sentence
                response = await self._ada.process_input_streaming(
                    text,
                    push_sentence_fn=self._push_sentence,
                    source_type="user_voice",
                )

                # Signal end of response — TTS flushes any remaining buffered text
                await self.push_frame(LLMFullResponseEndFrame())

                # Push Ada's response to the chat UI
                try:
                    from ui.server import broadcast_ada_response
                    full_response = " ".join(self._response_sentences) if self._response_sentences else response
                    if full_response:
                        asyncio.create_task(broadcast_ada_response(full_response))
                except ImportError:
                    pass

                if not response:
                    try:
                        self._state.transition_voice(VoiceState.ATTENTIVE)
                    except Exception:
                        self._state.voice = VoiceState.ATTENTIVE

            except Exception as e:
                log.error(f"Processing failed: {e}")
                await self.push_frame(LLMFullResponseStartFrame())
                await self.push_frame(TextFrame(text="Got it."))
                await self.push_frame(LLMFullResponseEndFrame())
            finally:
                self._processing = False

        elif isinstance(frame, UserStartedSpeakingFrame):
            # Barge-in: user started speaking while Ada might be talking
            from ada.state import VoiceState
            log.debug("User started speaking (barge-in)")
            try:
                self._state.voice = VoiceState.LISTENING
            except Exception:
                pass
            await self.push_frame(frame, direction)

        elif isinstance(frame, TTSStoppedFrame):
            # TTS playback finished — NOW transition to ATTENTIVE
            from ada.state import VoiceState
            if self._state.voice == VoiceState.SPEAKING:
                log.debug("TTS finished — transitioning to ATTENTIVE")
                try:
                    self._state.transition_voice(VoiceState.ATTENTIVE)
                except Exception:
                    self._state.voice = VoiceState.ATTENTIVE
            await self.push_frame(frame, direction)

        elif isinstance(frame, UserStoppedSpeakingFrame):
            log.debug("User stopped speaking")
            await self.push_frame(frame, direction)

        else:
            # Pass through all other frames
            await self.push_frame(frame, direction)


class VoicePipeline:
    """Manages the full Pipecat voice pipeline lifecycle."""

    def __init__(self, config: "AdaConfig", state: "SystemState", process_fn=None, ada_instance=None):
        self._config = config
        self._state = state
        self._process_fn = process_fn  # legacy, unused if ada_instance is set
        self._ada = ada_instance
        self._runner: PipelineRunner | None = None
        self._task: PipelineTask | None = None

    async def start(self) -> None:
        """Build and start the voice pipeline."""
        log.info("Starting voice pipeline...")

        vc = self._config.voice

        # --- Detect audio devices (config overrides auto-detection) ---
        if vc.input_device_index is not None or vc.output_device_index is not None:
            input_device = vc.input_device_index
            output_device = vc.output_device_index
            log.info(f"Audio devices from config — input: {input_device}, output: {output_device}")
        else:
            input_device, output_device = self._detect_audio_devices()
            log.info(f"Audio devices auto-detected — input: {input_device}, output: {output_device}")

        # --- Detect hardware sample rate ---
        # Many USB/built-in audio devices only support 44100/48000Hz.
        # We must open PyAudio at the hardware's native rate.
        # Pipecat, Whisper, and Kokoro handle internal resampling.
        hw_rate = self._get_device_rate(input_device, output_device)
        log.info(f"Hardware sample rate: {hw_rate}Hz")

        # Pipeline rate for VAD/STT (Silero VAD and Whisper both need 16kHz)
        pipeline_rate = 16000

        # --- Transport: local mic + speaker at hardware native rate ---
        # VAD is NOT on the transport — it runs after resampling in the pipeline.
        # Output resampling (TTS 24kHz → hw_rate) is handled by Pipecat's base_output.
        transport_params = LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=hw_rate,
            audio_out_sample_rate=hw_rate,
            input_device_index=input_device,
            output_device_index=output_device,
        )
        transport = LocalAudioTransport(transport_params)

        # --- Resampler: hw_rate → 16kHz for VAD + STT ---
        resampler = InputResampler(from_rate=hw_rate, to_rate=pipeline_rate)

        # --- VAD: Silero at 16kHz (after resampling) ---
        vad = VADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                sample_rate=pipeline_rate,
                params=VADParams(
                    confidence=0.6,
                    start_secs=0.2,
                    stop_secs=0.8,
                    min_volume=0.4,
                ),
            ),
        )

        # --- STT: faster-whisper on GPU ---
        stt = WhisperSTTService(
            settings=WhisperSTTSettings(
                model=vc.stt_model,
                language="en",
                no_speech_prob=0.4,
            ),
        )

        # --- TTS: Cloud (Microsoft Azure) with Kokoro CPU fallback ---
        try:
            from .cloud_tts import CloudTtsService
            tts = CloudTtsService(
                speech_key=getattr(self._config.tts, 'azure_speech_key', ''),
                speech_region=getattr(self._config.tts, 'azure_speech_region', 'eastus'),
                voice=getattr(self._config.tts, 'azure_voice', 'en-US-AvaMultilingualNeural'),
            )
            log.info("TTS: Microsoft Azure Cloud (neural voice)")
        except Exception as e:
            log.warning(f"Cloud TTS failed to init: {e} — falling back to Kokoro")
            tts = KokoroTTSService(
                settings=KokoroTTSSettings(
                    voice="af_heart",
                    language="en-us",
                ),
            )

        # --- Wake Word Gate (disabled — always attentive) ---
        # wakeword_gate = WakeWordGate(state=self._state, config=self._config.voice)

        # --- Ada Bridge: STT → Ada (streaming) → TTS ---
        bridge = AdaBridge(
            ada_instance=self._ada,
            state=self._state,
        )

        # --- Pipeline ---
        # Mic(44.1kHz) → Resample(16kHz) → VAD → STT → Bridge → VibeVoice TTS → Speaker(44.1kHz)
        pipeline = Pipeline([
            transport.input(),
            resampler,
            vad,
            stt,
            bridge,
            tts,
            transport.output(),
        ])

        self._task = PipelineTask(
            pipeline,
            params=PipelineParams(
                allow_interruptions=True,       # barge-in support
                enable_metrics=True,
            ),
        )

        self._runner = PipelineRunner()
        log.info("Voice pipeline ready. Listening...")

        await self._runner.run(self._task)

    @staticmethod
    def _get_device_rate(input_idx: int | None, output_idx: int | None) -> int:
        """Get a sample rate that works for both input and output devices.

        Tests common rates in preference order. Falls back to 44100 if detection fails.
        """
        import pyaudio
        p = pyaudio.PyAudio()
        try:
            preferred_rates = [16000, 24000, 44100, 48000]
            for rate in preferred_rates:
                input_ok = True
                output_ok = True
                if input_idx is not None:
                    try:
                        p.is_format_supported(rate, input_device=input_idx,
                                              input_channels=1, input_format=pyaudio.paInt16)
                    except ValueError:
                        input_ok = False
                if output_idx is not None:
                    try:
                        p.is_format_supported(rate, output_device=output_idx,
                                              output_channels=1, output_format=pyaudio.paInt16)
                    except ValueError:
                        output_ok = False
                if input_ok and output_ok:
                    return rate
            return 44100  # safe fallback
        finally:
            p.terminate()

    @staticmethod
    def _detect_audio_devices() -> tuple[int | None, int | None]:
        """Auto-detect the best input (mic) and output (speaker) device indices.

        Strategy: find the best input device, then prefer an output on the SAME
        hardware device (same card). This ensures headsets, USB audio interfaces,
        and combo devices work as a matched pair.

        Falls back to best-scored independent output if no same-device output exists.
        """
        import pyaudio
        p = pyaudio.PyAudio()

        SKIP_WORDS = {"hdmi", "spdif", "digital", "s/pdif"}
        MIC_WORDS = {"usb", "mic", "microphone", "capture", "webcam", "blue", "yeti", "rode", "headset"}

        devices: list[dict] = []
        try:
            for i in range(p.get_device_count()):
                try:
                    info = p.get_device_info_by_index(i)
                except Exception:
                    continue
                name = info["name"].lower()
                if any(w in name for w in ("null", "loop", "dummy", "virtual", "pipewire", "pulse")):
                    continue
                if name == "default":
                    continue
                devices.append({"idx": i, "name": info["name"], "lower": name,
                                "in_ch": info["maxInputChannels"], "out_ch": info["maxOutputChannels"],
                                "rate": int(info["defaultSampleRate"])})
        finally:
            p.terminate()

        # --- Score and pick best INPUT device ---
        input_candidates = []
        for d in devices:
            if d["in_ch"] <= 0:
                continue
            score = 0
            if any(w in d["lower"] for w in SKIP_WORDS):
                score -= 100
            if any(w in d["lower"] for w in MIC_WORDS):
                score += 50
            if d["in_ch"] <= 2:
                score += 10
            if "hw:" in d["lower"]:
                score += 5
            # Bonus: device also has output (headset/combo device — ideal for pairing)
            if d["out_ch"] > 0:
                score += 20
            # Tiebreaker: prefer lower sub-device index (primary endpoint on multi-endpoint cards)
            # e.g. hw:1,0 before hw:1,1 — the primary endpoint is usually the real mic
            input_candidates.append((score, -d["idx"], d["idx"], d["name"]))

        input_candidates.sort(reverse=True)
        input_idx = input_candidates[0][2] if input_candidates else None
        input_name = input_candidates[0][3] if input_candidates else ""

        # --- Pick OUTPUT: prefer same device as input (headset pairing) ---
        output_idx = None
        if input_idx is not None:
            # Check if the input device also has output channels
            for d in devices:
                if d["idx"] == input_idx and d["out_ch"] > 0:
                    output_idx = input_idx
                    log.info(f"Paired input+output on same device: [{input_idx}] {input_name}")
                    break

            # If input device has no output, look for same card (hw:N,*)
            if output_idx is None and "hw:" in input_name.lower():
                card = input_name.lower().split("hw:")[1].split(",")[0]
                for d in devices:
                    if d["out_ch"] > 0 and f"hw:{card}," in d["lower"] and d["idx"] != input_idx:
                        output_idx = d["idx"]
                        log.info(f"Paired output on same card: [{output_idx}] {d['name']}")
                        break

        # --- Fallback: best independent output ---
        if output_idx is None:
            output_candidates = []
            for d in devices:
                if d["out_ch"] <= 0:
                    continue
                score = 0
                if any(w in d["lower"] for w in SKIP_WORDS):
                    score -= 100
                if any(w in d["lower"] for w in ("speaker", "headphone", "analog", "usb", "pch", "realtek")):
                    score += 50
                if d["out_ch"] <= 2:
                    score += 10
                if "hw:" in d["lower"]:
                    score += 5
                output_candidates.append((score, d["idx"], d["name"]))
            output_candidates.sort(reverse=True)
            if output_candidates:
                output_idx = output_candidates[0][1]
                log.info(f"Output fallback: [{output_idx}] {output_candidates[0][2]}")

        if input_candidates:
            log.info(f"Input candidates: {[(s, idx, n) for s, _, idx, n in input_candidates[:3]]}")

        return input_idx, output_idx

    async def stop(self) -> None:
        """Stop the voice pipeline."""
        if self._task:
            await self._task.cancel()
        log.info("Voice pipeline stopped.")
