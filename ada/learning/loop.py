"""
Learning Loop — orchestrates Ada's self-improvement cycle.

Runs as a background task (overnight or on-demand). Pipeline:

    1. Mine high-quality (user, ada) pairs from episode history
    2. Filter by quality score (conversation flow heuristics)
    3. Train a LoRA adapter via QLoRA (fits 14B on 24GB GPU)
    4. Export to GGUF and create an Ollama model with the adapter
    5. Optionally swap Ada's voice model to the fine-tuned version

The learning loop is gated: it only runs if enough new pairs exist
and the training loss improves over the previous run.
"""

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .miner import extract_sft_pairs, SFTPair
from .trainer import LoRATrainer, LoRAConfig, TrainingResult

log = logging.getLogger(__name__)

LEARNING_STATE_FILE = os.path.expanduser("~/GitHub/AdaOS/checkpoints/learning_state.json")


@dataclass
class LearningState:
    """Tracks learning loop state across runs."""
    last_run: str = ""
    last_adapter_path: str = ""
    last_loss: float = 999.0
    total_runs: int = 0
    total_pairs_trained: int = 0
    active_model: str = ""  # Ollama model name if custom model is active


def _load_state() -> LearningState:
    if os.path.exists(LEARNING_STATE_FILE):
        with open(LEARNING_STATE_FILE) as f:
            data = json.load(f)
            return LearningState(**data)
    return LearningState()


def _save_state(state: LearningState) -> None:
    os.makedirs(os.path.dirname(LEARNING_STATE_FILE), exist_ok=True)
    with open(LEARNING_STATE_FILE, "w") as f:
        json.dump(state.__dict__, f, indent=2)


async def run_learning_loop(
    pool,
    min_quality: float = 0.6,
    min_pairs: int = 20,
    since_days: int = 90,
    config: LoRAConfig | None = None,
    export_to_ollama: bool = True,
) -> dict:
    """Run the full learning cycle.

    Args:
        pool: asyncpg connection pool (Ada's store)
        min_quality: quality threshold for SFT pairs
        min_pairs: minimum pairs needed to train
        since_days: look back window for episodes
        config: LoRA training config (uses defaults if None)
        export_to_ollama: if True, create an Ollama model from the adapter

    Returns:
        Dict with run results
    """
    state = _load_state()
    log.info(f"Learning loop starting (run #{state.total_runs + 1}, "
             f"last loss: {state.last_loss:.4f})")

    # ── Step 1: Mine training data ──
    pairs = await extract_sft_pairs(
        pool,
        min_quality=min_quality,
        limit=5000,
        since_days=since_days,
    )

    if len(pairs) < min_pairs:
        log.info(f"Not enough pairs ({len(pairs)} < {min_pairs}). Skipping.")
        return {"status": "skipped", "reason": "insufficient_pairs", "pairs": len(pairs)}

    # ── Step 2: Train LoRA adapter ──
    # Run training in executor to not block the event loop
    cfg = config or LoRAConfig()
    trainer = LoRATrainer(cfg)

    loop = asyncio.get_event_loop()
    result: TrainingResult = await loop.run_in_executor(None, trainer.train, pairs)

    if result.status != "completed":
        log.warning(f"Training {result.status}: {result.error}")
        return {"status": result.status, "error": result.error}

    # ── Step 3: Quality gate ──
    improved = result.final_loss < state.last_loss
    if not improved and state.total_runs > 0:
        log.warning(f"Loss did not improve ({result.final_loss:.4f} >= {state.last_loss:.4f}). "
                    "Keeping previous adapter.")
        # Don't reject on first run
        if state.total_runs > 1:
            return {
                "status": "rejected",
                "reason": "no_improvement",
                "loss": result.final_loss,
                "previous_loss": state.last_loss,
            }

    # ── Step 4: Export to Ollama (optional) ──
    ollama_model = ""
    if export_to_ollama:
        ollama_model = await _export_to_ollama(result.adapter_path, cfg.base_model)

    # ── Step 5: Update state ──
    state.last_run = datetime.now().isoformat()
    state.last_adapter_path = result.adapter_path
    state.last_loss = result.final_loss
    state.total_runs += 1
    state.total_pairs_trained += result.pairs_used
    if ollama_model:
        state.active_model = ollama_model
    _save_state(state)

    log.info(f"Learning loop complete. Loss: {result.final_loss:.4f}, "
             f"pairs: {result.pairs_used}, adapter: {result.adapter_path}")

    return {
        "status": "completed",
        "pairs_used": result.pairs_used,
        "final_loss": result.final_loss,
        "adapter_path": result.adapter_path,
        "duration_sec": result.duration_sec,
        "ollama_model": ollama_model,
        "improved": improved,
        "run_number": state.total_runs,
    }


async def _export_to_ollama(adapter_path: str, base_model: str) -> str:
    """Create an Ollama model from a LoRA adapter.

    Uses llama.cpp's export tools to merge adapter weights and create
    an Ollama-compatible model.

    Returns the Ollama model name, or empty string on failure.
    """
    model_name = "ada-tuned"
    modelfile_path = os.path.join(adapter_path, "Modelfile")

    # Check if conversion tools exist
    # For now, we create an Ollama Modelfile that references the adapter
    # Full GGUF conversion requires llama.cpp — we'll use Ollama's native
    # adapter loading if available, otherwise just save the Modelfile
    # for manual conversion

    # Determine base Ollama model name from HF model ID
    base_ollama = _hf_to_ollama_name(base_model)

    modelfile_content = f"""FROM {base_ollama}
ADAPTER {adapter_path}

PARAMETER temperature 0.7
PARAMETER top_p 0.9
PARAMETER num_ctx 4096

SYSTEM \"\"\"You are Ada, a voice-first AI assistant. You are helpful, concise, and natural in conversation. You learn from past interactions to better serve your user.\"\"\"
"""

    with open(modelfile_path, "w") as f:
        f.write(modelfile_content)

    log.info(f"Modelfile written to {modelfile_path}")

    # Try to create the Ollama model
    try:
        proc = await asyncio.create_subprocess_exec(
            "ollama", "create", model_name, "-f", modelfile_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            log.info(f"Ollama model '{model_name}' created successfully")
            return model_name
        else:
            log.warning(f"Ollama create failed: {stderr.decode()}")
            log.info(f"Modelfile saved at {modelfile_path} for manual conversion")
            return ""
    except FileNotFoundError:
        log.warning("Ollama not found. Modelfile saved for manual conversion.")
        return ""


def _hf_to_ollama_name(hf_model: str) -> str:
    """Map HuggingFace model ID to Ollama model name."""
    mapping = {
        "Qwen/Qwen3-14B": "qwen3:14b",
        "Qwen/Qwen3-32B": "qwen3:32b",
        "Qwen/Qwen3-8B": "qwen3:8b",
        "Qwen/Qwen3-1.7B": "qwen3:1.7b",
    }
    return mapping.get(hf_model, hf_model.split("/")[-1].lower())
