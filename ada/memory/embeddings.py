"""Local embedding generation — BAAI/bge-small-en-v1.5 (384-dim, CPU).

Loads once, stays in memory. Embeds text for episodes, memories, entities,
and search queries. No API dependency — fully local.
"""

import logging
from functools import lru_cache

log = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        log.info("Loading embedding model: BAAI/bge-small-en-v1.5")
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("BAAI/bge-small-en-v1.5")
        log.info("Embedding model loaded (384-dim, CPU)")
    return _model


_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def embed_sync(text: str, is_query: bool = False) -> list[float]:
    """Synchronous embedding — for use in sync contexts.

    For retrieval queries, set is_query=True to prepend the BGE
    instruction prefix, which significantly improves recall.
    """
    model = _get_model()
    input_text = _QUERY_PREFIX + text if is_query else text
    embedding = model.encode(input_text, normalize_embeddings=True)
    return embedding.tolist()


async def embed(text: str, is_query: bool = False) -> list[float]:
    """Async embedding — runs sync model in executor to avoid blocking event loop.

    Set is_query=True for search queries (adds BGE instruction prefix).
    Leave False for documents/memories being stored.
    """
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, embed_sync, text, is_query)


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Batch embedding — more efficient for multiple texts."""
    import asyncio

    def _batch():
        model = _get_model()
        embeddings = model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [e.tolist() for e in embeddings]

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _batch)
