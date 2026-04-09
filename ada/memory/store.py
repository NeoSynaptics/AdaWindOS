"""PostgreSQL store — async connection pool + query methods for all memory types.

All public methods handle connection errors gracefully:
- Log the error with context (which operation, what data)
- Raise StoreError so callers can decide how to degrade
- Connection pool auto-reconnects on next healthy query
"""

import json
import logging
from datetime import datetime
from typing import Any

import asyncpg

from ..config import DatabaseConfig
from .models import Task, Note, Episode, Memory, Entity, Relation, JournalEntry, OutboxEvent, Session

log = logging.getLogger(__name__)


class StoreError(Exception):
    """Raised when a store operation fails."""
    pass


def _vec(embedding: list[float] | str | None) -> str | None:
    """Convert a Python list of floats to pgvector string format."""
    if embedding is None:
        return None
    if isinstance(embedding, str):
        return embedding
    return "[" + ",".join(str(f) for f in embedding) + "]"


class Store:
    def __init__(self, config: DatabaseConfig):
        self.config = config
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        try:
            self.pool = await asyncpg.create_pool(
                host=self.config.host,
                port=self.config.port,
                database=self.config.database,
                user=self.config.user,
                password=self.config.password,
                min_size=self.config.min_pool_size,
                max_size=self.config.max_pool_size,
            )
            log.info(f"Connected to PostgreSQL at {self.config.host}:{self.config.port}/{self.config.database}")
        except (asyncpg.PostgresError, OSError) as e:
            log.error(f"Failed to connect to PostgreSQL: {e}")
            raise StoreError(f"Database connection failed: {e}") from e

    def _ensure_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise StoreError("Store not connected — call connect() first")
        return self.pool

    async def _exec(self, operation: str, coro):
        """Wrap a database operation with error handling and logging."""
        pool = self._ensure_pool()
        try:
            return await coro
        except asyncpg.PostgresError as e:
            log.error(f"Database error in {operation}: {e}", exc_info=True)
            raise StoreError(f"{operation} failed: {e}") from e
        except asyncpg.InterfaceError as e:
            log.error(f"Connection error in {operation}: {e} — pool may need reconnect", exc_info=True)
            raise StoreError(f"{operation} connection failed: {e}") from e

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            log.info("PostgreSQL connection pool closed")

    # --- Episodes ---

    async def insert_episode(self, ep: Episode) -> int:
        return await self.pool.fetchval(
            """INSERT INTO episodes (timestamp, session_id, turn_type, speaker, content, embedding, decision_event_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
            ep.timestamp, ep.session_id, ep.turn_type, ep.speaker,
            ep.content, _vec(ep.embedding), ep.decision_event_id,
        )

    async def get_recent_episodes(self, session_id: str, limit: int = 20) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT * FROM episodes WHERE session_id = $1 ORDER BY timestamp DESC LIMIT $2""",
            session_id, limit,
        )
        return [dict(r) for r in reversed(rows)]

    async def get_unconsolidated_episodes(self, limit: int = 100) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT * FROM episodes WHERE consolidated = FALSE ORDER BY timestamp ASC LIMIT $1""",
            limit,
        )
        return [dict(r) for r in rows]

    async def mark_consolidated(self, episode_ids: list[int]) -> None:
        await self.pool.execute(
            """UPDATE episodes SET consolidated = TRUE WHERE id = ANY($1)""",
            episode_ids,
        )

    # --- Memories (semantic) ---

    async def insert_memory(self, mem: Memory) -> int:
        return await self.pool.fetchval(
            """INSERT INTO memories (content, memory_type, confidence, source_episode_ids, embedding, valid_from)
               VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
            mem.content, mem.memory_type, mem.confidence,
            mem.source_episode_ids, _vec(mem.embedding), mem.valid_from,
        )

    async def search_memories(
        self,
        embedding: list[float],
        limit: int = 10,
        query_text: str | None = None,
        vector_weight: float = 0.7,
    ) -> list[dict]:
        """Hybrid search: vector similarity + BM25 keyword matching.

        When query_text is provided, combines pgvector cosine distance with
        ts_rank full-text score using Reciprocal Rank Fusion (RRF).  This
        catches jargon / keyword matches that pure vector search misses.

        When query_text is None, falls back to pure vector search.
        """
        if query_text is None:
            rows = await self.pool.fetch(
                """SELECT *, embedding <=> $1::vector AS distance
                   FROM memories WHERE valid_until IS NULL
                   ORDER BY distance ASC LIMIT $2""",
                _vec(embedding), limit,
            )
            return [dict(r) for r in rows]

        # Hybrid: RRF fusion of vector rank + keyword rank
        keyword_weight = 1.0 - vector_weight
        rows = await self.pool.fetch(
            """WITH vector_ranked AS (
                 SELECT *, embedding <=> $1::vector AS vec_dist,
                        ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector ASC) AS vec_rank
                 FROM memories WHERE valid_until IS NULL
                 ORDER BY vec_dist ASC LIMIT $2 * 3
               ),
               keyword_ranked AS (
                 SELECT id,
                        ts_rank_cd(to_tsvector('english', content), plainto_tsquery('english', $3)) AS kw_score,
                        ROW_NUMBER() OVER (
                          ORDER BY ts_rank_cd(to_tsvector('english', content), plainto_tsquery('english', $3)) DESC
                        ) AS kw_rank
                 FROM memories WHERE valid_until IS NULL
                   AND to_tsvector('english', content) @@ plainto_tsquery('english', $3)
               )
               SELECT v.*,
                      v.vec_dist AS distance,
                      COALESCE(k.kw_score, 0) AS kw_score,
                      ($4::float / (60 + v.vec_rank) + $5::float / (60 + COALESCE(k.kw_rank, 1000))) AS rrf_score
               FROM vector_ranked v
               LEFT JOIN keyword_ranked k ON v.id = k.id
               ORDER BY rrf_score DESC
               LIMIT $2""",
            _vec(embedding), limit, query_text, vector_weight, keyword_weight,
        )
        return [dict(r) for r in rows]

    async def invalidate_memory(self, memory_id: int) -> None:
        await self.pool.execute(
            """UPDATE memories SET valid_until = NOW(), updated_at = NOW() WHERE id = $1""",
            memory_id,
        )

    # --- Entities + Relations ---

    async def upsert_entity(self, entity: Entity) -> int:
        return await self.pool.fetchval(
            """INSERT INTO entities (name, entity_type, embedding)
               VALUES ($1, $2, $3)
               ON CONFLICT (name) DO UPDATE SET entity_type = $2, embedding = $3
               RETURNING id""",
            entity.name, entity.entity_type, _vec(entity.embedding),
        )

    async def insert_relation(self, rel: Relation) -> int:
        return await self.pool.fetchval(
            """INSERT INTO relations (subject_id, predicate, object_id, confidence, source_memory_id)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            rel.subject_id, rel.predicate, rel.object_id,
            rel.confidence, rel.source_memory_id,
        )

    async def get_entity_relations(self, entity_id: int, depth: int = 2) -> list[dict]:
        rows = await self.pool.fetch(
            """WITH RECURSIVE graph AS (
                 SELECT r.*, 1 AS depth FROM relations r
                 WHERE r.subject_id = $1 AND r.valid_until IS NULL
                 UNION ALL
                 SELECT r.*, g.depth + 1 FROM relations r
                 JOIN graph g ON r.subject_id = g.object_id
                 WHERE g.depth < $2 AND r.valid_until IS NULL
               )
               SELECT g.*, e.name AS object_name, e.entity_type AS object_type
               FROM graph g JOIN entities e ON g.object_id = e.id""",
            entity_id, depth,
        )
        return [dict(r) for r in rows]

    # --- Tasks ---

    async def insert_task(self, task: Task) -> None:
        await self.pool.execute(
            """INSERT INTO tasks (task_id, title, type, origin, status, priority, visibility,
               owner, created_by, brief, success_criteria, constraints, artifacts_expected,
               budget_class, cloud_budget_limit, dispatch_target, repo_path, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)""",
            task.task_id, task.title, task.type, task.origin, task.status,
            task.priority, task.visibility, task.owner, task.created_by,
            task.brief, json.dumps(task.success_criteria), json.dumps(task.constraints),
            json.dumps(task.artifacts_expected), task.budget_class,
            task.cloud_budget_limit, task.dispatch_target, task.repo_path,
            task.created_at, task.updated_at,
        )

    _TASK_UPDATABLE = frozenset({
        "status", "priority", "verdict", "result_summary", "error_message",
        "progress", "budget_class", "cloud_budget_limit", "dispatch_target",
        "repo_path", "artifacts_expected",
    })

    async def update_task_status(self, task_id: str, status: str, **kwargs) -> None:
        sets = ["status = $2", "updated_at = NOW()"]
        params: list[Any] = [task_id, status]
        i = 3
        for key, val in kwargs.items():
            if key not in self._TASK_UPDATABLE:
                raise ValueError(f"Column not allowed for update: {key}")
            sets.append(f"{key} = ${i}")
            params.append(val)
            i += 1
        await self.pool.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = $1", *params,
        )

    async def get_task(self, task_id: str) -> dict | None:
        row = await self.pool.fetchrow("SELECT * FROM tasks WHERE task_id = $1", task_id)
        return dict(row) if row else None

    async def get_active_tasks(self) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT * FROM tasks WHERE status NOT IN ('done', 'failed', 'cancelled')
               ORDER BY priority DESC, created_at ASC""",
        )
        return [dict(r) for r in rows]

    # --- Notes ---

    async def insert_note(self, note: Note) -> None:
        await self.pool.execute(
            """INSERT INTO notes (note_id, content, type, status, priority, source, owner, visibility, confidence)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            note.note_id, note.content, note.type, note.status,
            note.priority, note.source, note.owner, note.visibility, note.confidence,
        )

    _NOTE_UPDATABLE = frozenset({
        "status", "content", "type", "priority", "source", "owner",
        "visibility", "confidence",
    })

    async def update_note_status(self, note_id: str, status: str, **kwargs) -> None:
        sets = ["status = $2", "updated_at = NOW()"]
        params: list[Any] = [note_id, status]
        i = 3
        for key, val in kwargs.items():
            if key not in self._NOTE_UPDATABLE:
                raise ValueError(f"Column not allowed for update: {key}")
            sets.append(f"{key} = ${i}")
            params.append(val)
            i += 1
        await self.pool.execute(
            f"UPDATE notes SET {', '.join(sets)} WHERE note_id = $1", *params,
        )

    async def get_active_notes(self) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT * FROM notes WHERE status NOT IN ('archived')
               ORDER BY created_at DESC LIMIT 50""",
        )
        return [dict(r) for r in rows]

    # --- Decision Events ---

    async def insert_decision_event(self, event_dict: dict) -> None:
        ts = event_dict["timestamp"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        await self.pool.execute(
            """INSERT INTO decision_events (event_id, timestamp, sequence_num, source, classification, decision, execution, budget, meta)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            event_dict["event_id"], ts, event_dict["sequence_num"],
            json.dumps(event_dict["source"]), json.dumps(event_dict["classification"]),
            json.dumps(event_dict["decision"]), json.dumps(event_dict.get("execution")),
            json.dumps(event_dict.get("budget")), json.dumps(event_dict.get("meta")),
        )

    # --- Journal ---

    async def insert_journal(self, entry: JournalEntry) -> None:
        await self.pool.execute(
            """INSERT INTO journal (timestamp, action_type, action_summary, task_id, agent, band, budget_impact, rollback_hint, state_before, state_after, details)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            entry.timestamp, entry.action_type, entry.action_summary,
            entry.task_id, entry.agent, entry.band,
            json.dumps(entry.budget_impact), entry.rollback_hint,
            entry.state_before, entry.state_after, json.dumps(entry.details),
        )

    async def get_journal_today(self) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT * FROM journal WHERE timestamp >= CURRENT_DATE ORDER BY timestamp ASC""",
        )
        return [dict(r) for r in rows]

    async def get_cloud_tokens_today(self) -> int:
        row = await self.pool.fetchrow(
            """SELECT COALESCE(SUM((budget_impact->>'cloud_tokens')::int), 0) AS total
               FROM journal WHERE timestamp >= CURRENT_DATE AND budget_impact IS NOT NULL""",
        )
        return row["total"] if row else 0

    # --- Outbox ---

    async def insert_outbox(self, event: OutboxEvent) -> int:
        return await self.pool.fetchval(
            """INSERT INTO outbox_events (decision_event_id, event_type, payload, status, visible_at)
               VALUES ($1,$2,$3,$4,$5) RETURNING id""",
            event.decision_event_id, event.event_type,
            json.dumps(event.payload), event.status, event.visible_at,
        )

    async def fetch_pending_outbox(self, batch: int = 50) -> list[dict]:
        rows = await self.pool.fetch(
            """SELECT * FROM outbox_events
               WHERE status = 'pending' AND visible_at <= NOW()
               ORDER BY created_at ASC LIMIT $1""",
            batch,
        )
        return [dict(r) for r in rows]

    async def mark_outbox_processed(self, outbox_id: int) -> None:
        await self.pool.execute(
            """UPDATE outbox_events SET status = 'processed', processed_at = NOW() WHERE id = $1""",
            outbox_id,
        )

    async def bump_outbox_attempt(self, outbox_id: int, error: str) -> None:
        await self.pool.execute(
            """UPDATE outbox_events
               SET attempt = attempt + 1, error = $2,
                   visible_at = NOW() + (attempt * interval '30 seconds'),
                   status = CASE WHEN attempt + 1 >= max_attempts THEN 'failed' ELSE 'pending' END
               WHERE id = $1""",
            outbox_id, error,
        )

    # --- Sessions ---

    async def create_session(self, session_id: str) -> None:
        await self.pool.execute(
            "INSERT INTO sessions (session_id) VALUES ($1) ON CONFLICT DO NOTHING",
            session_id,
        )

    async def end_session(self, session_id: str) -> None:
        await self.pool.execute(
            "UPDATE sessions SET ended_at = NOW() WHERE session_id = $1",
            session_id,
        )

    # --- Health check ---

    async def health_check(self) -> bool:
        try:
            await self.pool.fetchval("SELECT 1")
            return True
        except Exception:
            return False
