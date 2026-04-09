"""SQLite store — zero-infra replacement for PostgreSQL.

AdaWindOS: All data stored in a single SQLite file. No Docker, no pgvector,
no TimescaleDB. Vector search uses numpy cosine similarity.

All public methods are async for API compatibility with the original store.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any

import numpy as np

from ..config import DatabaseConfig
from .models import Task, Note, Episode, Memory, Entity, Relation, JournalEntry, OutboxEvent, Session

log = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("ADA_DATA_DIR", Path.home() / ".ada" / "data"))


class StoreError(Exception):
    """Raised when a store operation fails."""
    pass


def _vec_to_bytes(embedding: list[float] | None) -> bytes | None:
    """Convert embedding list to numpy bytes for storage."""
    if embedding is None:
        return None
    return np.array(embedding, dtype=np.float32).tobytes()


def _bytes_to_vec(data: bytes | None) -> list[float] | None:
    """Convert stored bytes back to embedding list."""
    if data is None:
        return None
    return np.frombuffer(data, dtype=np.float32).tolist()


def _cosine_similarity(a: list[float], b: bytes | None) -> float:
    """Cosine similarity between a query vector and stored bytes."""
    if b is None:
        return 0.0
    va = np.array(a, dtype=np.float32)
    vb = np.frombuffer(b, dtype=np.float32)
    dot = np.dot(va, vb)
    norm = np.linalg.norm(va) * np.linalg.norm(vb)
    if norm == 0:
        return 0.0
    return float(dot / norm)


class Store:
    def __init__(self, config: DatabaseConfig = None):
        self.config = config
        self.db: sqlite3.Connection | None = None
        self._db_path = DATA_DIR / "ada.db"

    async def connect(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self._create_tables()
            log.info(f"Connected to SQLite at {self._db_path}")
        except Exception as e:
            log.error(f"Failed to connect to SQLite: {e}")
            raise StoreError(f"Database connection failed: {e}") from e

    def _create_tables(self):
        """Create all tables if they don't exist."""
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                session_id TEXT,
                turn_type TEXT,
                speaker TEXT,
                content TEXT,
                embedding BLOB,
                decision_event_id TEXT,
                consolidated INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                memory_type TEXT,
                confidence REAL DEFAULT 0.8,
                source_episode_ids TEXT DEFAULT '[]',
                embedding BLOB,
                valid_from TEXT DEFAULT (datetime('now')),
                valid_until TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                entity_type TEXT,
                embedding BLOB,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject_id INTEGER REFERENCES entities(id),
                predicate TEXT,
                object_id INTEGER REFERENCES entities(id),
                confidence REAL DEFAULT 0.8,
                source_memory_id INTEGER,
                valid_until TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT,
                type TEXT,
                origin TEXT,
                status TEXT DEFAULT 'queued',
                priority INTEGER DEFAULT 5,
                visibility TEXT DEFAULT 'internal',
                owner TEXT,
                created_by TEXT,
                brief TEXT,
                success_criteria TEXT DEFAULT '[]',
                constraints TEXT DEFAULT '[]',
                artifacts_expected TEXT DEFAULT '[]',
                budget_class TEXT DEFAULT 'standard',
                cloud_budget_limit INTEGER,
                dispatch_target TEXT,
                repo_path TEXT,
                verdict TEXT,
                result_summary TEXT,
                error_message TEXT,
                progress REAL,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS notes (
                note_id TEXT PRIMARY KEY,
                content TEXT,
                type TEXT DEFAULT 'general',
                status TEXT DEFAULT 'active',
                priority INTEGER DEFAULT 5,
                source TEXT,
                owner TEXT DEFAULT 'daniel',
                visibility TEXT DEFAULT 'personal',
                confidence REAL DEFAULT 1.0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS decision_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                timestamp TEXT,
                sequence_num INTEGER,
                source TEXT,
                classification TEXT,
                decision TEXT,
                execution TEXT,
                budget TEXT,
                meta TEXT
            );

            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                action_type TEXT,
                action_summary TEXT,
                task_id TEXT,
                agent TEXT,
                band INTEGER,
                budget_impact TEXT,
                rollback_hint TEXT,
                state_before TEXT,
                state_after TEXT,
                details TEXT
            );

            CREATE TABLE IF NOT EXISTS outbox_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_event_id TEXT,
                event_type TEXT,
                payload TEXT,
                status TEXT DEFAULT 'pending',
                visible_at TEXT DEFAULT (datetime('now')),
                created_at TEXT DEFAULT (datetime('now')),
                processed_at TEXT,
                attempt INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 3,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                started_at TEXT DEFAULT (datetime('now')),
                ended_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id);
            CREATE INDEX IF NOT EXISTS idx_episodes_consolidated ON episodes(consolidated);
            CREATE INDEX IF NOT EXISTS idx_memories_valid ON memories(valid_until);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(status);
            CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_events(status, visible_at);
            CREATE INDEX IF NOT EXISTS idx_journal_date ON journal(timestamp);
        """)

    def _ensure_db(self) -> sqlite3.Connection:
        if self.db is None:
            raise StoreError("Store not connected — call connect() first")
        return self.db

    async def close(self) -> None:
        if self.db:
            self.db.close()
            log.info("SQLite connection closed")

    # --- Episodes ---

    async def insert_episode(self, ep: Episode) -> int:
        db = self._ensure_db()
        cur = db.execute(
            """INSERT INTO episodes (timestamp, session_id, turn_type, speaker, content, embedding, decision_event_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ep.timestamp or datetime.now().isoformat(), ep.session_id, ep.turn_type,
             ep.speaker, ep.content, _vec_to_bytes(ep.embedding), ep.decision_event_id),
        )
        db.commit()
        return cur.lastrowid

    async def get_recent_episodes(self, session_id: str, limit: int = 20) -> list[dict]:
        db = self._ensure_db()
        rows = db.execute(
            "SELECT * FROM episodes WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    async def get_unconsolidated_episodes(self, limit: int = 100) -> list[dict]:
        db = self._ensure_db()
        rows = db.execute(
            "SELECT * FROM episodes WHERE consolidated = 0 ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    async def mark_consolidated(self, episode_ids: list[int]) -> None:
        db = self._ensure_db()
        placeholders = ",".join("?" for _ in episode_ids)
        db.execute(f"UPDATE episodes SET consolidated = 1 WHERE id IN ({placeholders})", episode_ids)
        db.commit()

    # --- Memories (semantic) ---

    async def insert_memory(self, mem: Memory) -> int:
        db = self._ensure_db()
        source_ids = json.dumps(mem.source_episode_ids) if mem.source_episode_ids else "[]"
        cur = db.execute(
            """INSERT INTO memories (content, memory_type, confidence, source_episode_ids, embedding, valid_from)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (mem.content, mem.memory_type, mem.confidence,
             source_ids, _vec_to_bytes(mem.embedding), mem.valid_from or datetime.now().isoformat()),
        )
        db.commit()
        return cur.lastrowid

    async def search_memories(
        self,
        embedding: list[float],
        limit: int = 10,
        query_text: str | None = None,
        vector_weight: float = 0.7,
    ) -> list[dict]:
        """Search memories by cosine similarity (numpy, no pgvector)."""
        db = self._ensure_db()
        rows = db.execute(
            "SELECT * FROM memories WHERE valid_until IS NULL"
        ).fetchall()

        if not rows:
            return []

        # Score each memory by cosine similarity
        scored = []
        for row in rows:
            d = dict(row)
            sim = _cosine_similarity(embedding, d.get("embedding"))
            d["distance"] = 1.0 - sim
            d["similarity"] = sim

            # Keyword boost if query_text provided
            if query_text:
                content_lower = (d.get("content") or "").lower()
                query_lower = query_text.lower()
                keyword_hits = sum(1 for word in query_lower.split() if word in content_lower)
                keyword_score = keyword_hits / max(len(query_lower.split()), 1)
                d["score"] = vector_weight * sim + (1 - vector_weight) * keyword_score
            else:
                d["score"] = sim

            scored.append(d)

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    async def invalidate_memory(self, memory_id: int) -> None:
        db = self._ensure_db()
        db.execute(
            "UPDATE memories SET valid_until = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (memory_id,),
        )
        db.commit()

    # --- Entities + Relations ---

    async def upsert_entity(self, entity: Entity) -> int:
        db = self._ensure_db()
        cur = db.execute(
            """INSERT INTO entities (name, entity_type, embedding) VALUES (?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET entity_type = ?, embedding = ?""",
            (entity.name, entity.entity_type, _vec_to_bytes(entity.embedding),
             entity.entity_type, _vec_to_bytes(entity.embedding)),
        )
        db.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = db.execute("SELECT id FROM entities WHERE name = ?", (entity.name,)).fetchone()
        return row["id"] if row else 0

    async def insert_relation(self, rel: Relation) -> int:
        db = self._ensure_db()
        cur = db.execute(
            """INSERT INTO relations (subject_id, predicate, object_id, confidence, source_memory_id)
               VALUES (?, ?, ?, ?, ?)""",
            (rel.subject_id, rel.predicate, rel.object_id, rel.confidence, rel.source_memory_id),
        )
        db.commit()
        return cur.lastrowid

    async def get_entity_relations(self, entity_id: int, depth: int = 2) -> list[dict]:
        db = self._ensure_db()
        # Simple non-recursive query for SQLite (recursive CTEs supported but keep simple)
        rows = db.execute(
            """SELECT r.*, e.name AS object_name, e.entity_type AS object_type
               FROM relations r JOIN entities e ON r.object_id = e.id
               WHERE r.subject_id = ? AND r.valid_until IS NULL""",
            (entity_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Tasks ---

    async def insert_task(self, task: Task) -> None:
        db = self._ensure_db()
        db.execute(
            """INSERT INTO tasks (task_id, title, type, origin, status, priority, visibility,
               owner, created_by, brief, success_criteria, constraints, artifacts_expected,
               budget_class, cloud_budget_limit, dispatch_target, repo_path, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (task.task_id, task.title, task.type, task.origin, task.status,
             task.priority, task.visibility, task.owner, task.created_by,
             task.brief, json.dumps(task.success_criteria), json.dumps(task.constraints),
             json.dumps(task.artifacts_expected), task.budget_class,
             task.cloud_budget_limit, task.dispatch_target, task.repo_path,
             task.created_at or datetime.now().isoformat(),
             task.updated_at or datetime.now().isoformat()),
        )
        db.commit()

    _TASK_UPDATABLE = frozenset({
        "status", "priority", "verdict", "result_summary", "error_message",
        "progress", "budget_class", "cloud_budget_limit", "dispatch_target",
        "repo_path", "artifacts_expected",
    })

    async def update_task_status(self, task_id: str, status: str, **kwargs) -> None:
        db = self._ensure_db()
        sets = ["status = ?", "updated_at = datetime('now')"]
        params: list[Any] = [status]
        for key, val in kwargs.items():
            if key not in self._TASK_UPDATABLE:
                raise ValueError(f"Column not allowed for update: {key}")
            sets.append(f"{key} = ?")
            params.append(val)
        params.append(task_id)
        db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ?", params)
        db.commit()

    async def get_task(self, task_id: str) -> dict | None:
        db = self._ensure_db()
        row = db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    async def get_active_tasks(self) -> list[dict]:
        db = self._ensure_db()
        rows = db.execute(
            """SELECT * FROM tasks WHERE status NOT IN ('done', 'failed', 'cancelled')
               ORDER BY priority DESC, created_at ASC""",
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Notes ---

    async def insert_note(self, note: Note) -> None:
        db = self._ensure_db()
        db.execute(
            """INSERT INTO notes (note_id, content, type, status, priority, source, owner, visibility, confidence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (note.note_id, note.content, note.type, note.status,
             note.priority, note.source, note.owner, note.visibility, note.confidence),
        )
        db.commit()

    _NOTE_UPDATABLE = frozenset({
        "status", "content", "type", "priority", "source", "owner",
        "visibility", "confidence",
    })

    async def update_note_status(self, note_id: str, status: str, **kwargs) -> None:
        db = self._ensure_db()
        sets = ["status = ?", "updated_at = datetime('now')"]
        params: list[Any] = [status]
        for key, val in kwargs.items():
            if key not in self._NOTE_UPDATABLE:
                raise ValueError(f"Column not allowed for update: {key}")
            sets.append(f"{key} = ?")
            params.append(val)
        params.append(note_id)
        db.execute(f"UPDATE notes SET {', '.join(sets)} WHERE note_id = ?", params)
        db.commit()

    async def get_active_notes(self) -> list[dict]:
        db = self._ensure_db()
        rows = db.execute(
            "SELECT * FROM notes WHERE status NOT IN ('archived') ORDER BY created_at DESC LIMIT 50",
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Decision Events ---

    async def insert_decision_event(self, event_dict: dict) -> None:
        db = self._ensure_db()
        ts = event_dict.get("timestamp", datetime.now().isoformat())
        if isinstance(ts, datetime):
            ts = ts.isoformat()
        db.execute(
            """INSERT INTO decision_events (event_id, timestamp, sequence_num, source, classification, decision, execution, budget, meta)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (event_dict["event_id"], ts, event_dict["sequence_num"],
             json.dumps(event_dict["source"]), json.dumps(event_dict["classification"]),
             json.dumps(event_dict["decision"]), json.dumps(event_dict.get("execution")),
             json.dumps(event_dict.get("budget")), json.dumps(event_dict.get("meta"))),
        )
        db.commit()

    # --- Journal ---

    async def insert_journal(self, entry: JournalEntry) -> None:
        db = self._ensure_db()
        db.execute(
            """INSERT INTO journal (timestamp, action_type, action_summary, task_id, agent, band, budget_impact, rollback_hint, state_before, state_after, details)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (entry.timestamp or datetime.now().isoformat(), entry.action_type, entry.action_summary,
             entry.task_id, entry.agent, entry.band,
             json.dumps(entry.budget_impact) if entry.budget_impact else None,
             entry.rollback_hint, entry.state_before, entry.state_after,
             json.dumps(entry.details) if entry.details else None),
        )
        db.commit()

    async def get_journal_today(self) -> list[dict]:
        db = self._ensure_db()
        today = date.today().isoformat()
        rows = db.execute(
            "SELECT * FROM journal WHERE timestamp >= ? ORDER BY timestamp ASC",
            (today,),
        ).fetchall()
        return [dict(r) for r in rows]

    async def get_cloud_tokens_today(self) -> int:
        db = self._ensure_db()
        today = date.today().isoformat()
        row = db.execute(
            """SELECT COALESCE(SUM(json_extract(budget_impact, '$.cloud_tokens')), 0) AS total
               FROM journal WHERE timestamp >= ? AND budget_impact IS NOT NULL""",
            (today,),
        ).fetchone()
        return row["total"] if row else 0

    # --- Outbox ---

    async def insert_outbox(self, event: OutboxEvent) -> int:
        db = self._ensure_db()
        cur = db.execute(
            """INSERT INTO outbox_events (decision_event_id, event_type, payload, status, visible_at)
               VALUES (?,?,?,?,?)""",
            (event.decision_event_id, event.event_type,
             json.dumps(event.payload), event.status,
             event.visible_at or datetime.now().isoformat()),
        )
        db.commit()
        return cur.lastrowid

    async def fetch_pending_outbox(self, batch: int = 50) -> list[dict]:
        db = self._ensure_db()
        now = datetime.now().isoformat()
        rows = db.execute(
            """SELECT * FROM outbox_events
               WHERE status = 'pending' AND visible_at <= ?
               ORDER BY created_at ASC LIMIT ?""",
            (now, batch),
        ).fetchall()
        return [dict(r) for r in rows]

    async def mark_outbox_processed(self, outbox_id: int) -> None:
        db = self._ensure_db()
        db.execute(
            "UPDATE outbox_events SET status = 'processed', processed_at = datetime('now') WHERE id = ?",
            (outbox_id,),
        )
        db.commit()

    async def bump_outbox_attempt(self, outbox_id: int, error: str) -> None:
        db = self._ensure_db()
        db.execute(
            """UPDATE outbox_events
               SET attempt = attempt + 1, error = ?,
                   visible_at = datetime('now', '+' || (attempt * 30) || ' seconds'),
                   status = CASE WHEN attempt + 1 >= max_attempts THEN 'failed' ELSE 'pending' END
               WHERE id = ?""",
            (error, outbox_id),
        )
        db.commit()

    # --- Sessions ---

    async def create_session(self, session_id: str) -> None:
        db = self._ensure_db()
        db.execute(
            "INSERT OR IGNORE INTO sessions (session_id) VALUES (?)",
            (session_id,),
        )
        db.commit()

    async def end_session(self, session_id: str) -> None:
        db = self._ensure_db()
        db.execute(
            "UPDATE sessions SET ended_at = datetime('now') WHERE session_id = ?",
            (session_id,),
        )
        db.commit()

    # --- Health check ---

    async def health_check(self) -> bool:
        try:
            self._ensure_db().execute("SELECT 1")
            return True
        except Exception:
            return False
