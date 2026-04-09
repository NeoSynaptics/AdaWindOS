"""
LoRA Fine-Tuner — trains a LoRA adapter on Ada's conversation history.

Adapted from OpenJarvis's learning primitive. Uses PEFT + transformers to train
a lightweight LoRA adapter that can be loaded into Ollama via GGUF export.

Pipeline:
    Episodes (pgvector) → Miner (quality filter) → LoRA trainer → GGUF → Ollama
"""

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .miner import SFTPair, format_chat_pairs

log = logging.getLogger(__name__)

CHECKPOINTS_DIR = os.path.expanduser("~/GitHub/AdaOS/checkpoints")


@dataclass
class LoRAConfig:
    """LoRA training configuration."""
    # Model
    base_model: str = "Qwen/Qwen3-14B"  # HuggingFace model ID

    # LoRA hyperparameters
    lora_rank: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    target_modules: list[str] = field(default_factory=lambda: [
        "q_proj", "k_proj", "v_proj", "o_proj",
    ])

    # Training
    num_epochs: int = 3
    batch_size: int = 2
    gradient_accumulation_steps: int = 4  # effective batch = 8
    learning_rate: float = 2e-5
    weight_decay: float = 0.01
    warmup_ratio: float = 0.1
    max_grad_norm: float = 1.0
    max_seq_length: int = 2048

    # Memory optimization
    use_4bit: bool = True  # QLoRA — fits 14B on single 24GB GPU
    gradient_checkpointing: bool = True
    bf16: bool = True

    # Output
    output_dir: str = CHECKPOINTS_DIR
    min_pairs: int = 20  # minimum SFT pairs to start training


@dataclass
class TrainingResult:
    status: str  # "completed", "skipped", "error"
    pairs_used: int = 0
    epochs: int = 0
    final_loss: float = 0.0
    adapter_path: str = ""
    duration_sec: float = 0.0
    error: str = ""


class LoRATrainer:
    """Trains a LoRA adapter from Ada's conversation pairs."""

    def __init__(self, config: LoRAConfig | None = None):
        self.config = config or LoRAConfig()

    def train(self, pairs: list[SFTPair]) -> TrainingResult:
        """Run LoRA fine-tuning on extracted SFT pairs.

        This is a blocking call (runs on GPU). Should be called from
        a background task during off-hours.
        """
        if len(pairs) < self.config.min_pairs:
            log.info(f"Only {len(pairs)} pairs (need {self.config.min_pairs}). Skipping.")
            return TrainingResult(status="skipped", pairs_used=len(pairs))

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
            from peft import LoraConfig as PeftLoraConfig, get_peft_model, TaskType
        except ImportError as e:
            return TrainingResult(status="error", error=f"Missing dependency: {e}")

        start = datetime.now()
        cfg = self.config
        chat_data = format_chat_pairs(pairs)

        # ── Tokenize ──
        log.info(f"Loading tokenizer: {cfg.base_model}")
        tokenizer = AutoTokenizer.from_pretrained(cfg.base_model, trust_remote_code=True)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token

        dataset = []
        for item in chat_data:
            try:
                text = tokenizer.apply_chat_template(
                    item["conversations"], tokenize=False, add_generation_prompt=False,
                )
            except Exception:
                # Fallback: manual formatting
                msgs = item["conversations"]
                text = f"<|user|>\n{msgs[0]['content']}\n<|assistant|>\n{msgs[1]['content']}{tokenizer.eos_token}"

            encoded = tokenizer(
                text,
                max_length=cfg.max_seq_length,
                truncation=True,
                padding="max_length",
                return_tensors="pt",
            )
            dataset.append({
                "input_ids": encoded["input_ids"].squeeze(0),
                "attention_mask": encoded["attention_mask"].squeeze(0),
            })

        log.info(f"Tokenized {len(dataset)} training examples")

        # ── Load model ──
        log.info(f"Loading base model: {cfg.base_model}")
        model_kwargs = {
            "trust_remote_code": True,
            "device_map": "auto",
        }

        if cfg.use_4bit:
            model_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
            )
        elif cfg.bf16:
            model_kwargs["torch_dtype"] = torch.bfloat16

        model = AutoModelForCausalLM.from_pretrained(cfg.base_model, **model_kwargs)

        if cfg.gradient_checkpointing:
            model.gradient_checkpointing_enable(
                gradient_checkpointing_kwargs={"use_reentrant": False}
            )

        # ── Apply LoRA ──
        peft_config = PeftLoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=cfg.lora_rank,
            lora_alpha=cfg.lora_alpha,
            lora_dropout=cfg.lora_dropout,
            target_modules=cfg.target_modules,
        )
        model = get_peft_model(model, peft_config)
        trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
        total = sum(p.numel() for p in model.parameters())
        log.info(f"LoRA applied: {trainable:,} trainable / {total:,} total "
                 f"({100 * trainable / total:.2f}%)")

        # ── Train ──
        optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=cfg.learning_rate,
            weight_decay=cfg.weight_decay,
        )

        # Simple linear warmup
        total_steps = (len(dataset) // cfg.batch_size) * cfg.num_epochs
        warmup_steps = int(total_steps * cfg.warmup_ratio)

        model.train()
        global_step = 0
        total_loss = 0.0

        for epoch in range(cfg.num_epochs):
            epoch_loss = 0.0
            # Shuffle
            import random
            random.shuffle(dataset)

            for i in range(0, len(dataset), cfg.batch_size):
                batch_items = dataset[i:i + cfg.batch_size]
                if not batch_items:
                    continue

                input_ids = torch.stack([b["input_ids"] for b in batch_items]).to(model.device)
                attention_mask = torch.stack([b["attention_mask"] for b in batch_items]).to(model.device)

                outputs = model(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    labels=input_ids,
                )
                loss = outputs.loss / cfg.gradient_accumulation_steps
                loss.backward()

                if (global_step + 1) % cfg.gradient_accumulation_steps == 0:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), cfg.max_grad_norm)
                    optimizer.step()
                    optimizer.zero_grad()

                epoch_loss += loss.item() * cfg.gradient_accumulation_steps
                global_step += 1

            avg_epoch_loss = epoch_loss / max(1, len(dataset) // cfg.batch_size)
            log.info(f"Epoch {epoch + 1}/{cfg.num_epochs} — loss: {avg_epoch_loss:.4f}")
            total_loss = avg_epoch_loss

        # ── Save adapter ──
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        adapter_path = os.path.join(cfg.output_dir, f"ada_lora_{timestamp}")
        os.makedirs(adapter_path, exist_ok=True)
        model.save_pretrained(adapter_path)
        tokenizer.save_pretrained(adapter_path)

        # Save training metadata
        meta = {
            "base_model": cfg.base_model,
            "pairs_used": len(pairs),
            "epochs": cfg.num_epochs,
            "final_loss": total_loss,
            "lora_rank": cfg.lora_rank,
            "timestamp": timestamp,
            "avg_quality": sum(p.quality_score for p in pairs) / len(pairs),
        }
        with open(os.path.join(adapter_path, "training_meta.json"), "w") as f:
            json.dump(meta, f, indent=2)

        duration = (datetime.now() - start).total_seconds()
        log.info(f"Training complete. Adapter saved to {adapter_path} "
                 f"({duration:.0f}s, loss={total_loss:.4f})")

        return TrainingResult(
            status="completed",
            pairs_used=len(pairs),
            epochs=cfg.num_epochs,
            final_loss=total_loss,
            adapter_path=adapter_path,
            duration_sec=duration,
        )
