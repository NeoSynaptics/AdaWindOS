"""
Training Data Miner — extracts SFT pairs from Ada's episode history.

Inspired by OpenJarvis's learning primitive, adapted for Ada's pgvector store.
Ada's episodes are conversation turns: (timestamp, speaker, content, turn_type).
We pair consecutive (user → ada) turns into training examples, then filter by
quality signals derived from the conversation flow.
"""

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class SFTPair:
    """A single supervised fine-tuning example."""
    user_message: str
    ada_response: str
    session_id: str
    timestamp: str
    quality_score: float  # 0.0–1.0


def _estimate_quality(user_msg: str, ada_msg: str, episodes_in_session: list[dict]) -> float:
    """Heuristic quality score for a (user, ada) pair.

    Signals that indicate a GOOD response:
    - Ada gave a substantive answer (not just "I don't know")
    - The user continued the conversation (didn't immediately correct Ada)
    - The response has reasonable length (not too short, not padded)
    - No error/fallback phrases in Ada's response

    Returns 0.0–1.0.
    """
    score = 0.5  # baseline

    # Length signals
    ada_len = len(ada_msg)
    if ada_len < 20:
        score -= 0.3  # too short, probably a fallback
    elif ada_len > 100:
        score += 0.1  # substantive response
    if ada_len > 2000:
        score -= 0.1  # probably over-explained

    # Error/fallback detection
    bad_phrases = [
        "i don't know", "i'm not sure", "sorry, i can't",
        "error", "failed to", "something went wrong",
        "i couldn't", "let me try again",
    ]
    ada_lower = ada_msg.lower()
    for phrase in bad_phrases:
        if phrase in ada_lower:
            score -= 0.2
            break

    # User didn't correct Ada (no immediate "no", "wrong", "that's not")
    # Look at the next user message in the session
    correction_phrases = ["no,", "no ", "wrong", "that's not", "not what i",
                          "actually,", "i meant", "try again"]
    # (We'd need the next turn to check this — handled in extract_pairs)

    # User message quality — skip trivial inputs
    user_len = len(user_msg)
    if user_len < 5:
        score -= 0.2  # "ok", "yes", etc. — not useful for SFT

    return max(0.0, min(1.0, score))


async def extract_sft_pairs(
    pool,
    min_quality: float = 0.6,
    limit: int = 5000,
    since_days: int = 90,
) -> list[SFTPair]:
    """Extract (user, ada) conversation pairs from episode history.

    Args:
        pool: asyncpg connection pool
        min_quality: minimum quality score threshold
        limit: max pairs to extract
        since_days: only look at episodes from the last N days

    Returns:
        List of SFTPair objects sorted by quality (highest first)
    """
    # Fetch all episodes from the time window, grouped by session
    rows = await pool.fetch(
        """SELECT session_id, turn_type, speaker, content, timestamp
           FROM episodes
           WHERE timestamp > NOW() - INTERVAL '%s days'
           ORDER BY session_id, timestamp ASC""" % since_days,
    )

    if not rows:
        log.info("No episodes found for SFT extraction")
        return []

    # Group by session
    sessions: dict[str, list[dict]] = {}
    for r in rows:
        sid = r["session_id"]
        if sid not in sessions:
            sessions[sid] = []
        sessions[sid].append(dict(r))

    # Extract pairs: user turn followed by ada turn
    pairs: list[SFTPair] = []
    seen: set[tuple[str, str]] = set()  # dedup

    for sid, turns in sessions.items():
        for i in range(len(turns) - 1):
            curr = turns[i]
            nxt = turns[i + 1]

            # Must be user → ada pair
            if curr["turn_type"] != "user" or nxt["turn_type"] != "ada":
                continue

            user_msg = (curr["content"] or "").strip()
            ada_msg = (nxt["content"] or "").strip()

            # Skip empty
            if not user_msg or not ada_msg:
                continue

            # Dedup
            key = (user_msg[:100], ada_msg[:100])
            if key in seen:
                continue
            seen.add(key)

            # Quality scoring
            quality = _estimate_quality(user_msg, ada_msg, turns)

            # Check if user corrected Ada in the next turn
            if i + 2 < len(turns) and turns[i + 2]["turn_type"] == "user":
                next_user = (turns[i + 2]["content"] or "").lower()
                correction_phrases = ["no,", "no ", "wrong", "that's not",
                                      "not what i", "actually,", "i meant", "try again"]
                if any(next_user.startswith(p) or p in next_user[:50] for p in correction_phrases):
                    quality -= 0.25

            if quality >= min_quality:
                pairs.append(SFTPair(
                    user_message=user_msg,
                    ada_response=ada_msg,
                    session_id=sid,
                    timestamp=str(curr["timestamp"]),
                    quality_score=quality,
                ))

    # Sort by quality, take top N
    pairs.sort(key=lambda p: p.quality_score, reverse=True)
    pairs = pairs[:limit]

    log.info(f"Extracted {len(pairs)} SFT pairs (min_quality={min_quality}, "
             f"from {len(sessions)} sessions, {len(rows)} episodes)")
    return pairs


def format_chat_pairs(pairs: list[SFTPair]) -> list[dict]:
    """Convert SFTPairs to chat-format training data.

    Returns list of dicts with 'conversations' key for tokenizer.apply_chat_template().
    """
    return [
        {
            "conversations": [
                {"role": "user", "content": p.user_message},
                {"role": "assistant", "content": p.ada_response},
            ],
            "quality": p.quality_score,
        }
        for p in pairs
    ]
