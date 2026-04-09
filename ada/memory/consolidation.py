"""Memory consolidation — the brain's "sleep replay".

Periodically reads unconsolidated episodes, uses the LLM to extract
facts/preferences/decisions/patterns, compares against existing memories
(vector similarity), and writes new or updates existing.

This is what makes Ada learn over time. Without it, Ada has episodic
memory (what happened) but no semantic memory (what she knows).

Based on Complementary Learning Systems theory:
- Hippocampus (episodes) → fast, specific, timestamped
- Neocortex (memories) → slow, general, consolidated
- Replay (this process) → bridges them
"""

import json
import logging
from datetime import datetime

import httpx

from .store import Store
from .models import Memory, Entity, Relation

log = logging.getLogger(__name__)

EXTRACTION_PROMPT = """You are a memory consolidation engine. Read the following conversation episodes and extract structured knowledge.

EPISODES:
{episodes}

Extract ALL of the following (if present). Return JSON:
{{
  "facts": [
    {{"content": "...", "type": "fact|preference|decision|pattern|correction", "confidence": 0.0-1.0}}
  ],
  "entities": [
    {{"name": "...", "type": "project|person|technology|concept"}}
  ],
  "relations": [
    {{"subject": "...", "predicate": "uses|depends_on|prefers|owns|contradicts", "object": "...", "confidence": 0.0-1.0}}
  ]
}}

Rules:
- Only extract what is clearly stated or strongly implied
- "preference" = Daniel expressed a choice ("I want X", "let's go with Y")
- "decision" = a final choice was made ("go with streaming pipeline")
- "pattern" = recurring behavior ("Daniel usually brainstorms before dispatching")
- "correction" = something changed ("was batch, now streaming")
- Set confidence based on how explicit the statement was
- If nothing to extract, return empty arrays"""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "type": {"type": "string", "enum": ["fact", "preference", "decision", "pattern", "correction"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                },
                "required": ["content", "type", "confidence"]
            }
        },
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["project", "person", "technology", "concept"]}
                },
                "required": ["name", "type"]
            }
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string", "enum": ["uses", "depends_on", "prefers", "owns", "contradicts"]},
                    "object": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                },
                "required": ["subject", "predicate", "object", "confidence"]
            }
        }
    },
    "required": ["facts", "entities", "relations"]
}


class ConsolidationEngine:
    def __init__(
        self,
        store: Store,
        gateway=None,
        ollama_base_url: str = "",
        model: str = "",
        embed_fn=None,
        similarity_threshold: float = 0.85,
    ):
        self.store = store
        self.gateway = gateway
        self.ollama_base_url = ollama_base_url
        self.model = model
        self.embed_fn = embed_fn
        self.similarity_threshold = similarity_threshold

    async def run(self, batch_size: int = 50) -> dict:
        """Run one consolidation pass. Returns stats."""
        stats = {"episodes_processed": 0, "facts_extracted": 0,
                 "entities_created": 0, "relations_created": 0,
                 "memories_updated": 0, "memories_new": 0}

        # 1. Get unconsolidated episodes
        episodes = await self.store.get_unconsolidated_episodes(limit=batch_size)
        if not episodes:
            log.debug("No unconsolidated episodes")
            return stats

        stats["episodes_processed"] = len(episodes)
        log.info(f"Consolidating {len(episodes)} episodes")

        # 2. Format episodes for LLM
        episode_text = self._format_episodes(episodes)

        # 3. Extract knowledge via LLM
        extraction = await self._extract(episode_text)
        if not extraction:
            log.warning("Extraction returned nothing")
            await self.store.mark_consolidated([ep["id"] for ep in episodes])
            return stats

        # 4. Process facts → memories (each fact handled independently — one failure doesn't block others)
        episode_ids = [ep["id"] for ep in episodes]
        for fact in extraction.get("facts", []):
            try:
                existed = await self._upsert_memory(fact, episode_ids)
                if existed:
                    stats["memories_updated"] += 1
                else:
                    stats["memories_new"] += 1
                stats["facts_extracted"] += 1
            except Exception as e:
                log.error(f"Failed to upsert memory '{fact.get('content', '')[:80]}': {e}", exc_info=True)

        # 5. Process entities (each independently)
        entity_id_map = {}
        for ent in extraction.get("entities", []):
            try:
                entity = Entity(name=ent["name"], entity_type=ent["type"])
                if self.embed_fn:
                    try:
                        entity.embedding = await self.embed_fn(ent["name"])
                    except Exception as e:
                        log.warning(f"Embedding failed for entity '{ent['name']}': {e}")
                        entity.embedding = None
                eid = await self.store.upsert_entity(entity)
                entity_id_map[ent["name"]] = eid
                stats["entities_created"] += 1
            except Exception as e:
                log.error(f"Failed to upsert entity '{ent.get('name', '')}': {e}", exc_info=True)

        # 6. Process relations (each independently)
        for rel in extraction.get("relations", []):
            try:
                subj_id = entity_id_map.get(rel["subject"])
                obj_id = entity_id_map.get(rel["object"])
                if subj_id and obj_id:
                    relation = Relation(
                        subject_id=subj_id,
                        predicate=rel["predicate"],
                        object_id=obj_id,
                        confidence=rel.get("confidence", 0.8),
                    )
                    await self.store.insert_relation(relation)
                    stats["relations_created"] += 1
                else:
                    log.debug(f"Skipping relation — missing entity: {rel['subject']} → {rel['object']}")
            except Exception as e:
                log.error(f"Failed to insert relation {rel}: {e}", exc_info=True)

        # 7. Mark episodes as consolidated
        await self.store.mark_consolidated(episode_ids)

        log.info(f"Consolidation complete: {stats}")
        return stats

    async def _extract(self, episode_text: str) -> dict | None:
        """Use LLM to extract structured knowledge from episodes.

        Uses APU gateway if available, falls back to direct httpx.
        """
        prompt = EXTRACTION_PROMPT.format(episodes=episode_text)

        try:
            if self.gateway:
                return await self.gateway.chat_json(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    schema=EXTRACTION_SCHEMA,
                    temperature=0.0,
                    timeout=60.0,
                )
            else:
                # Fallback: direct httpx (for when APU is disabled)
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        f"{self.ollama_base_url}/api/chat",
                        json={
                            "model": self.model,
                            "messages": [{"role": "user", "content": prompt}],
                            "format": EXTRACTION_SCHEMA,
                            "stream": False,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    return json.loads(data["message"]["content"])
        except Exception as e:
            log.error(f"Extraction failed: {e}")
            return None

    async def _upsert_memory(self, fact: dict, source_episode_ids: list[int]) -> bool:
        """Insert new memory or update existing if similar enough.

        Returns True if an existing memory was updated, False if new.
        """
        content = fact["content"]
        if not self.embed_fn:
            # Without embeddings, just insert as new
            mem = Memory(
                content=content,
                memory_type=fact["type"],
                confidence=fact["confidence"],
                source_episode_ids=source_episode_ids,
                embedding=[0.0] * 384,  # placeholder
            )
            await self.store.insert_memory(mem)
            return False

        embedding = await self.embed_fn(content)

        # Check for similar existing memory
        similar = await self.store.search_memories(embedding, limit=3)

        for existing in similar:
            distance = existing.get("distance", 1.0)
            similarity = 1.0 - distance

            if similarity >= self.similarity_threshold:
                # Memory exists and is similar — check if it needs updating
                if fact["type"] == "correction" or fact["type"] == "decision":
                    # Invalidate old memory, create new one (temporal knowledge)
                    await self.store.invalidate_memory(existing["id"])
                    mem = Memory(
                        content=content,
                        memory_type=fact["type"],
                        confidence=fact["confidence"],
                        source_episode_ids=source_episode_ids,
                        embedding=embedding,
                    )
                    await self.store.insert_memory(mem)
                    log.info(f"Memory updated (superseded): {content[:80]}")
                    return True
                else:
                    # Similar fact already exists — skip (NOOP)
                    log.debug(f"Memory already exists (similarity={similarity:.2f}): {content[:80]}")
                    return True

        # No similar memory — insert as new
        mem = Memory(
            content=content,
            memory_type=fact["type"],
            confidence=fact["confidence"],
            source_episode_ids=source_episode_ids,
            embedding=embedding,
        )
        await self.store.insert_memory(mem)
        log.info(f"New memory: [{fact['type']}] {content[:80]}")
        return False

    def _format_episodes(self, episodes: list[dict]) -> str:
        """Format episodes for the extraction prompt."""
        lines = []
        for ep in episodes:
            ts = ep["timestamp"]
            speaker = ep["speaker"]
            content = ep["content"]
            lines.append(f"[{ts}] {speaker}: {content}")
        return "\n".join(lines)
