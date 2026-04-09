"""
UltraPlan — Overnight batch planning daemon for AdaOS.

Inspired by Anthropic's ULTRAPLAN (leaked Claude Code feature flag):
  Cloud Opus, 30-min planning budget, browser approval, teleport back to local.

Our version: fully local, multi-GPU overnight, multi-pass self-critique,
zero API cost. Queue tasks before bed, wake up to validated plans.

Hardware strategy:
  - Daytime: GPUs busy with voice pipeline (Whisper + Qwen 14B)
  - Overnight: both GPUs free → run Qwen 72B (or larger) for deep planning
  - Time budget: 8 hours vs Anthropic's 30 minutes

Architecture:
  daemon.py      — Overnight loop, pulls from queue, orchestrates passes
  planner.py     — Multi-pass planning (decompose → critique → synthesize)
  queue.py       — SQLite-backed task queue (submit, claim, complete, review)
  templates.py   — Structured planning prompts (per domain, per pass)
  reviewer.py    — Morning review interface (CLI + optional noteboard integration)
  config.py      — Model selection, GPU mapping, timing, pass count
"""
