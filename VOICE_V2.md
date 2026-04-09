# Voice V2 — Streaming Pipeline with Sesame CSM + Granite STT + Mistral Nemo

The V1 voice pipeline proved the Pipecat architecture works end-to-end. V2 upgrades every component for production quality.

## Why V2

V1 problems:
1. **Amnesia** — conversation history buried in system prompt text, model doesn't see real turns
2. **Batch latency** — waits for full LLM response, then full TTS audio, then plays (10-15s delay)
3. **Generic personality** — model defaults to "How can I assist you?" every turn
4. **Kokoro voice quality** — functional but robotic (4.2 MOS)
5. **No streaming** — entire pipeline is blocking

V2 targets:
- First audio in Daniel's ears **~1-2s** after he stops talking
- Consistent, warm voice identity across all turns
- Conversation memory that actually works
- Personality that feels like a person, not a chatbot

---

## The Stack

| Component | V1 (current) | V2 (target) | Why |
|-----------|-------------|-------------|-----|
| STT | faster-whisper large-v3 | **Granite 4.0 1B Speech** | #1 OpenASR, smaller, Apache 2.0 |
| Brain (classify) | qwen3:14b | **qwen3:14b** (keep) | Strong JSON schema, 15 intents |
| Brain (respond) | qwen3:14b | **Mistral Nemo 12B** | Warmest conversational tone at this tier |
| TTS | Kokoro 82M (CPU) | **Sesame CSM 1B** (GPU) | 4.7 MOS, streaming, conversational |
| TTS fallback | none | **Kokoro 82M** (CPU) | If Sesame unavailable |
| Pipeline | blocking AdaBridge | **streaming Pipecat** | Sentence-by-sentence TTS |
| Context | history-in-system-prompt | **multi-turn messages** | Real conversation memory |

## Hardware Budget

```
RTX 5060 Ti (16GB):
  Mistral Nemo 12B Q4_K_M     ~8GB   (response generation)
  Qwen3 14B Q4_K_M            ~9GB   (classification — hot-swapped by APU)
  
RTX 4070 (12GB):
  Granite 4.0 1B Speech        ~2GB   (STT, RESIDENT)
  Sesame CSM 1B                ~8GB   (TTS, RESIDENT)
  Total:                       ~10GB

CPU (128GB RAM):
  Kokoro 82M                   ~200MB (fallback TTS)
  bge-small-en embeddings      ~100MB
```

---

## Implementation Steps

### Step 1: Fix Amnesia (no new models needed)

**Problem**: `_generate_response()` sends 2 messages: `{system: wall_of_text}, {user: current_turn}`.
The LLM sees history as part of the system prompt, not as a conversation.

**Fix**: Build proper multi-turn messages from episodes:

```python
# BEFORE (broken)
messages = [
    {"role": "system", "content": system_prompt + "\n# HISTORY\nDaniel: hello\nAda: hi..."},
    {"role": "user", "content": "The user said: X. Respond briefly."},
]

# AFTER (correct)
messages = [
    {"role": "system", "content": personality_prompt + context_block},
    {"role": "user", "content": "Hey Ada"},           # episode
    {"role": "assistant", "content": "Morning!"},      # episode
    {"role": "user", "content": "What's the status?"}, # episode
    {"role": "assistant", "content": "Three tasks..."},# episode
    {"role": "user", "content": "current turn"},       # new input
]
```

Context (tasks, notes, memories) goes in a compact block in the system prompt.
History goes as actual message turns. The LLM sees a real conversation.

**Files**: `ada/main.py` (`_generate_response`), `ada/memory/context_builder.py`

---

### Step 2: Personality Prompt

**Problem**: Generic instruction "Respond briefly, warmly, under 2 sentences. Be natural." every turn.

**Fix**: Permanent personality in system prompt with few-shot examples and explicit anti-patterns.

```
You are Ada. You are Daniel's executive AI — warm, direct, no fluff.

RULES:
- Use Daniel's name naturally (not every turn)
- Acknowledge FAST ("Got it", "On it", "Done")
- Summarize, don't list. One sentence beats three.
- When you don't know, say "I don't know" — never make it up
- Reference prior conversation naturally ("like you mentioned earlier...")

NEVER SAY:
- "How can I assist you today?"
- "Is there anything else?"
- "I'm here to help"
- "Feel free to ask"
- Any variation of offering help unprompted

EXAMPLES:
Daniel: "Hey Ada"
Ada: "Morning! Overnight, Arch finished the TTS evaluation — Sesame CSM won. Want the summary?"

Daniel: "Save this — we should test the camera input with the 4070"
Ada: "Noted. I'll flag it for your next session with Forge."

Daniel: "What's going on with tasks?"
Ada: "Two active. The dashboard eval is with Arch, should be back in an hour. The auth question is still in draft — want me to send that to Arch too?"

Daniel: "Thanks, that's it"
Ada: "Cool. I'll ping you when Arch delivers."
```

**Files**: `ada/system_prompt.py`, `ada/main.py`

---

### Step 3: Streaming LLM Response

**Problem**: AdaBridge calls `ada.process_input()` which blocks until the full LLM response is generated, then sends the entire text to TTS at once.

**Fix**: Split the pipeline into fast-path (classify + act) and streaming-path (generate + speak).

```
Option A: Keep AdaBridge, add streaming internally
  - AdaBridge calls classify + resolve (fast, <100ms)
  - Then streams response via Ollama streaming API
  - Buffers tokens until sentence boundary (".", "!", "?")
  - Pushes each sentence as TextFrame → Kokoro/Sesame generates audio
  - Audio starts playing while next sentence is still being generated

Option B: Use Pipecat's native OllamaLLMService
  - Replace AdaBridge with Pipecat's OLLamaLLMService processor
  - Use LLMUserContextAggregator for context management
  - Pipecat handles streaming + sentence aggregation natively
  - More complex to wire with Ada's decision engine
```

**Recommended: Option A** — keeps Ada's decision engine intact, just makes response generation streaming.

**Files**: `ada/voice/pipeline.py` (`AdaBridge`), `ada/main.py` (`_generate_response_streaming`)

---

### Step 4: Sesame CSM TTS

**Install**: `pip install sesame-csm` or clone from HuggingFace

**Key requirement**: Consistent voice identity. Sesame CSM generates speech conditioned on a speaker embedding.

```python
# Generate Ada's speaker embedding ONCE from a reference clip
# Cache it and reuse for every TTS call

ada_voice_embedding = csm.encode_speaker("ada_reference_voice.wav")
# Save to disk: ada/voice/ada_speaker.pt

# Every TTS call:
audio = csm.generate(
    text="Morning, Daniel!",
    speaker=ada_voice_embedding,  # same embedding every time
    context=[],  # or prior audio for better continuity
)
```

**Voice selection**: Pick a reference audio clip that sounds like the Ada we want — warm, female, clear, slight energy. Generate the embedding once, ship it with the repo.

**Pipecat integration**: Write a `SesameCsmTtsService` processor:
- Receives TextFrame
- Calls CSM with fixed speaker embedding
- Pushes OutputAudioRawFrame
- Handles streaming (CSM supports chunk-by-chunk generation)

**VRAM**: ~8GB on RTX 4070. Shares GPU with Granite STT (~2GB). Total ~10GB, fits in 12GB.

**Files**: `ada/voice/sesame_tts.py` (new), `ada/voice/pipeline.py`

---

### Step 5: Granite 4.0 1B STT

**Install**: HuggingFace Transformers (`ibm-granite/granite-4.0-1b-speech`)

**Why replace Whisper**: 
- #1 on OpenASR leaderboard (WER 5.52 avg)
- 1B params vs Whisper large-v3's 1.5B — smaller footprint
- Apache 2.0 license
- Multilingual (EN, FR, DE, ES, PT, JP)

**Pipecat integration**: Write a `GraniteSttService` processor:
- Receives InputAudioRawFrame (16kHz after resampling)
- Runs Granite inference
- Pushes TranscriptionFrame
- Handles streaming transcription if supported

**VRAM**: ~2GB on RTX 4070. Resident, never evicted.

**Files**: `ada/voice/granite_stt.py` (new), `ada/voice/pipeline.py`

---

### Step 6: Mistral Nemo 12B

**Install**: `ollama pull mistral-nemo:12b`

**Why**: Warmest conversational tone at this parameter count. Streams well via Ollama.

**Integration**: Just change `config.models.response_model` to `"mistral-nemo:12b"`. 
APU handles loading/unloading. Ollama serves it same as Qwen.

**Classification stays on Qwen3 14B** — it's better at structured JSON.
APU swaps between them: classify on Qwen → swap → respond on Nemo (~3-5s swap).
Or: test if Nemo can do both (classification + response). If yes, skip the swap.

**Files**: `ada/config.py`, `ada/main.py`

---

## Execution Order

```
1. Fix amnesia (multi-turn messages)          — immediate UX win, no new deps
2. Personality prompt                          — immediate UX win, no new deps  
3. Pull + test Mistral Nemo 12B               — ollama pull, test conversational quality
4. Streaming LLM response                     — biggest latency improvement
5. Install + test Sesame CSM                   — biggest quality improvement
6. Sesame CSM Pipecat integration              — wire into pipeline with fixed voice
7. Install + test Granite STT                  — replace Whisper
8. Granite STT Pipecat integration             — wire into pipeline
9. End-to-end streaming test                   — full pipeline: Granite → Nemo → Sesame
```

Steps 1-3 work with the current V1 pipeline and improve UX immediately.
Steps 4-8 are the V2 architecture swap.
Step 9 validates everything works together.

---

## Voice Identity Contract

Ada's voice MUST be consistent. Every turn sounds like the same person.

- Speaker embedding generated from a reference audio clip
- Cached at `ada/voice/ada_speaker.pt`
- Loaded once at pipeline startup
- Passed to every Sesame CSM call
- Never regenerated mid-session
- If Sesame is unavailable, Kokoro fallback uses its own fixed voice ("af_heart")

To change Ada's voice: replace the reference clip, regenerate the embedding, restart.

---

## Latency Budget

```
Current V1:
  STT:           ~350ms (Whisper)
  Classification: ~500ms (Qwen, often 10-15s on timeout)
  Response gen:   ~5-10s (full response, blocking)
  TTS:            ~340ms (Kokoro, full audio)
  Total:          ~8-15s to first audio

Target V2:
  STT:            ~200ms (Granite, smaller model)
  Classification: ~100ms (Qwen, warm model)
  First token:    ~200ms (Mistral Nemo, streaming)
  Sentence ready: ~500ms (5-10 tokens to first period)
  TTS first chunk:~200ms (Sesame CSM, streaming)
  Total:          ~1.2s to first audio
```

---

## Fallback Chain

```
Sesame CSM down → Kokoro TTS (CPU, automatic)
Granite STT down → Whisper large-v3 (already installed)
Mistral Nemo down → Qwen3 14B (already installed)
All models down → text-only mode (keyboard input/output)
```
