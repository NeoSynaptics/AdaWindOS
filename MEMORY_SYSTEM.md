# Memory System

Ada's memory architecture is brain-inspired, separating fast episodic recording from slow semantic consolidation. All data lives in PostgreSQL 16 with pgvector and TimescaleDB extensions.

## Architecture

```
Voice Input → Episode (raw turn) → Consolidation Engine → Memory (extracted fact)
                                                              ↓
                                                   Entity-Relation Graph
```

### Brain Analogy

| Brain System | Memory Type | AdaOS Table | Purpose |
|---|---|---|---|
| Hippocampus | Episodic | `episodes` | Raw conversation turns, timestamped |
| Neocortex | Semantic | `memories` | Extracted facts, preferences, decisions |
| Association cortex | Relational | `entities` + `relations` | Knowledge graph |
| Sleep replay | Consolidation | `ConsolidationEngine` | Bridges episodic → semantic |

## Tables

### episodes (TimescaleDB hypertable)

Every conversation turn is stored as an episode with its embedding vector.

| Column | Type | Description |
|--------|------|-------------|
| `id` | BIGSERIAL | Auto-increment |
| `timestamp` | TIMESTAMPTZ | When the turn happened |
| `session_id` | TEXT | Links turns within a session |
| `turn_type` | TEXT | `user` or `ada` |
| `speaker` | TEXT | `Daniel` or `Ada` |
| `content` | TEXT | Raw text |
| `embedding` | VECTOR(384) | BAAI/bge-small-en-v1.5 embedding |
| `decision_event_id` | TEXT | Links to the Decision Event that processed this |
| `consolidated` | BOOLEAN | Has this been processed by consolidation? |

Indexed with HNSW for vector similarity search. TimescaleDB hypertable for efficient time-range queries.

### memories (semantic facts)

Extracted knowledge with temporal validity.

| Column | Type | Description |
|--------|------|-------------|
| `id` | SERIAL | Primary key |
| `content` | TEXT | The fact or preference |
| `memory_type` | TEXT | `fact`, `preference`, `decision`, `pattern`, `correction` |
| `confidence` | FLOAT | 0.0-1.0, how explicit the source was |
| `source_episode_ids` | BIGINT[] | Which episodes this was extracted from |
| `embedding` | VECTOR(384) | For semantic search |
| `valid_from` | TIMESTAMPTZ | When this became true |
| `valid_until` | TIMESTAMPTZ | NULL = still valid; set when superseded |

Only memories with `valid_until IS NULL` are considered current. Corrections and new decisions invalidate old memories.

### entities + relations (knowledge graph)

```sql
-- entities: named things Ada knows about
entities(id, name, entity_type, embedding)
-- types: project, person, technology, concept

-- relations: how entities connect
relations(subject_id, predicate, object_id, confidence, valid_from, valid_until, source_memory_id)
-- predicates: uses, depends_on, prefers, owns, contradicts
```

Queried with recursive CTEs for multi-hop traversal:

```sql
WITH RECURSIVE graph AS (
    SELECT r.*, 1 AS depth FROM relations r WHERE r.subject_id = $1
    UNION ALL
    SELECT r.*, g.depth + 1 FROM relations r JOIN graph g ON r.subject_id = g.object_id
    WHERE g.depth < $2
)
SELECT ... FROM graph JOIN entities ON ...
```

## Consolidation Engine

Runs every 30 minutes (when Ada is not in a live conversation turn). Implements the "sleep replay" that bridges episodic to semantic memory.

### Process

1. **Fetch** unconsolidated episodes (batch of 50)
2. **Format** episodes as timestamped conversation text
3. **Extract** structured knowledge via LLM (Qwen 14B, JSON Schema constrained):
   - Facts, preferences, decisions, patterns, corrections
   - Named entities and their types
   - Relations between entities
4. **Deduplicate** via vector similarity (threshold: 0.85 cosine similarity):
   - Similar fact exists → skip (already known)
   - Similar fact exists but new one is a `correction` or `decision` → invalidate old, insert new
   - No similar fact → insert as new memory
5. **Update** entity-relation graph
6. **Mark** episodes as consolidated

### Extraction Prompt

The LLM extracts structured JSON:

```json
{
  "facts": [
    {"content": "SportWave uses LD2451 sensor", "type": "fact", "confidence": 0.95}
  ],
  "entities": [
    {"name": "SportWave", "type": "project"}
  ],
  "relations": [
    {"subject": "SportWave", "predicate": "uses", "object": "LD2451", "confidence": 0.95}
  ]
}
```

### Memory Types

| Type | When Extracted | Example |
|------|---------------|---------|
| `fact` | Stated information | "SportWave uses LD2451 sensor" |
| `preference` | User expressed a choice | "Daniel prefers streaming over batch" |
| `decision` | Final choice was made | "Going with Pipecat for voice pipeline" |
| `pattern` | Recurring behavior | "Daniel brainstorms before dispatching" |
| `correction` | Something changed | "Was SQLite, now PostgreSQL" |

## Context Builder

Assembles Ada's system prompt by pulling relevant context from memory before each response.

### For Classification

Lightweight context (fast, ~10ms):
- Active topic from recent episodes
- Pending tasks summary
- Last classified intent

### For Response Generation

Full context (richer, ~50ms):
- Recent conversation history
- Relevant memories (vector search on current input)
- Active notes and tasks
- Entity relations for mentioned topics

## Embeddings

- **Model:** BAAI/bge-small-en-v1.5 (384 dimensions)
- **Runs on:** CPU (no GPU needed)
- **Used for:** Episode embeddings, memory embeddings, entity embeddings, semantic search
- **Index:** HNSW (pgvector) for approximate nearest neighbor search

## Store

`ada/memory/store.py` provides an async interface over asyncpg with:

- Connection pool (2-10 connections)
- Graceful error handling (logs + raises `StoreError`)
- Auto-reconnect via pool
- Methods for all CRUD operations across all tables
- Health check endpoint

## Database Setup

```bash
# Start PostgreSQL with TimescaleDB + pgvector
docker compose up -d

# Schema auto-applied from ada/memory/schema.sql via Docker init
# Manual apply if needed:
psql -h localhost -U ada -d ada -f ada/memory/schema.sql
```

## Implementation Status

- Store (CRUD operations): complete
- Episode recording: complete
- Memory search (vector similarity): complete
- Consolidation engine: complete
- Entity-relation graph: complete
- Context builder: complete
- Embeddings: complete
