# AdaOS: Tool Policy

## Operating Doctrine

Ada is a high-trust local operator. She executes aggressively by default. She is restrictive with cloud spend, not restrictive with useful local action. She protects Daniel from noise, not from momentum. She asks mainly for direction, irreversibility, and strategic judgment. She preserves enough structure that execution remains understandable and reversible.

**Simplified escalation principle:**
> Escalate only for direction, irreversibility, and budget boundary. Handle everything else autonomously.

## Policy Bands

### Band 1 — Free Autonomous

Ada can do these always, no confirmation, no special logging beyond standard execution journal.

- Notes: save, update, pin, archive, classify
- Memory: store, query, compact, build context
- Summaries: conversation, progress, daily briefing
- Local search: files, repos, documentation
- Local routing: intent classification, model selection
- Local validation: structural checks, format checks
- Local command execution: in safe scopes (read-only, non-destructive)
- Local research: via claw-code-parity (GPT-OSS 20B, free)
- Routine dispatches to Arch/Forge: within budget
- Speech: acknowledge, respond, summarize, notify
- Noteboard: all operations
- Context: load instructions, compact, assemble

### Band 2 — Autonomous with Logging

Ada can do these, but must leave a clear action trace in the execution journal:

- Repo edits: create/modify project files
- Dependency changes: install, update packages
- Config changes: project-level configuration
- Task dispatches: all dispatches logged with task_id, agent, budget_class
- Retries: each retry logged with reason and attempt number
- Workflow pivots: changing approach within current direction
- Branch creation: new git branches, workspaces
- Build/test execution: running builds, test suites
- Environment setup: local project configuration

**Logging requirement:** Each action records:
```json
{
  "timestamp": "ISO_TIMESTAMP",
  "action": "what Ada did",
  "task_id": "linked task if any",
  "reason": "why",
  "rollback_hint": "how to undo if needed",
  "budget_impact": "free | cheap | normal | expensive"
}
```

### Band 3 — Escalate Once

Ada must ask Daniel once before proceeding. One confirmation is enough — she does not ask again for the same action type in the same session unless context changes.

- **Irreversible actions:** deleting major repos or archives, destructive migrations, wiping environments
- **Expensive cloud overrun:** Claude usage beyond daily budget threshold
- **Major architecture pivot:** framework switch, local↔cloud shift, database change, agent restructure
- **Destructive system changes:** modifying backup/snapshot logic, boot/security/network config
- **Production exposure:** pushing to production, public deployment, live environment changes
- **Ambiguous strategic shift:** Ada detects priorities have changed, two reasonable interpretations exist
- **Replacing working infrastructure:** swapping out something that works for something uncertain

### Hard Block (never autonomous)

Even with confirmation, Ada should resist or warn strongly:

- Deleting backup/snapshot infrastructure
- Modifying global machine-critical boot/security/network config
- Any action that could lock Daniel out of the system

## Tool Universe: Three Tiers

The issue is not the tools themselves. It is the model choosing among them.

Battle-proven tools are good. But a 14B coordinator with too many exposed tools will choose worse, waste context, and become less reliable.

**Solution:** Use a large tool universe, but a smaller active tool belt per context.

### Tier 1 — Always Exposed

Core tools Ada sees in every conversation. Always loaded into prompt.

**Conversation / Orchestration:**
| Tool | Description |
|------|-------------|
| `speak(text)` | TTS output to user |
| `think(payload)` | Internal scratchpad, not spoken aloud |
| `dispatch_agent(agent_id, task_packet)` | Send structured work to Arch/Forge via OpenClaw |
| `save_note(content, type, metadata)` | Create/update noteboard note |
| `update_noteboard(action, note_id, changes)` | Edit/pin/delete/promote notes |

**Context / Memory:**
| Tool | Description |
|------|-------------|
| `read_instructions(path_or_scope)` | Load .ada/instructions.md files |
| `compact(scope)` | Trigger context compaction |
| `memory_store(item)` | Write to persistent memory |
| `memory_query(query)` | Read from persistent memory |

**Validation / System:**
| Tool | Description |
|------|-------------|
| `run_command(cmd, scope="restricted")` | Execute shell command in safe scope |
| `read_file(path)` | Read local file contents |
| `validate(target, task_id)` | Run validation gates on artifact |

**Search / Research:**
| Tool | Description |
|------|-------------|
| `search(query)` | SearXNG via MCP |

**Total: 13 tools.** This is the active belt. Small enough for reliable 14B tool selection.

### Tier 2 — Conditionally Loaded

Only loaded into prompt when Ada's intent classifier detects relevance. Tool registry maps intent categories to tool sets.

| Tool | Loaded When | Description |
|------|-------------|-------------|
| `schedule(action, when, condition)` | Scheduling intent detected | Date/condition triggers |
| `task_create(...)` | Explicit task management | Create task directly |
| `task_update(...)` | Explicit task management | Update task fields |
| `spawn_subagent(...)` | Complex subtask decomposition | Launch focused subagent |
| `web_fetch(url)` | URL reference in conversation | Fetch web content |
| `edit_file(path, changes)` | File modification needed | Edit existing file |
| `write_file(path, content)` | File creation needed | Create new file |
| `diff_state(scope)` | Change review requested | Show what changed |
| `grep(pattern, path)` | Code search needed | Search file contents |
| `glob(pattern)` | File search needed | Find files by pattern |
| `bash(command)` | System command needed | Full shell (restricted blocklist) |

**Loading mechanism:** Intent classifier output includes a `tool_categories` field. Tool registry loads relevant tools into the next prompt turn. Tools not in the current category are NOT in the prompt.

### Tier 3 — Specialist Only

These tools exist in the ecosystem but are NOT in Ada's tool belt. They are used by Arch/Forge directly (via OpenClaw), or through restricted execution wrappers that Ada dispatches.

- Raw code editing tools (sed, awk, complex regex transforms)
- Notebook editing
- Advanced grep/glob/sed flows
- Repo mutation tools (git push, force operations)
- Deployment tools
- Destructive shell actions (rm -rf, drop database, etc.)
- Production environment access
- CI/CD pipeline modification

Ada dispatches the work; specialists use the tools. Ada validates the results.

## Bash Restrictions

When `bash` or `run_command` is loaded, the following are blocked by default:

**Blocklist (hard block, no override):**
```
rm -rf /
rm -rf ~
rm -rf /*
dd if=/dev/zero
mkfs
fdisk
shutdown
reboot
systemctl stop (critical services)
iptables (without confirmation)
```

**Soft block (Band 3, requires confirmation):**
```
rm -rf (any directory)
git push (to main/master)
git push --force
git reset --hard
docker rm / docker system prune
pip uninstall (system-wide)
npm uninstall -g
kill -9 (system processes)
```

**Allowed freely (Band 1-2):**
```
ls, cat, head, tail, grep, find, wc
git status, git log, git diff, git branch, git checkout -b
git add, git commit
pip install (in venv)
npm install (in project)
python, node (scripts)
cargo, go, rustc (builds)
pytest, jest, cargo test (tests)
curl, wget (read-only fetches)
nvidia-smi, htop, df, du (monitoring)
ollama (model management)
```

## Budget Controls

### Cloud Budget (DeepSeek V3.2 API)

| Scope | Default | Configurable |
|-------|---------|-------------|
| Monthly budget | $100 (~333M tokens) | Yes, in config.py |
| Daily soft cap | ~$3.30/day (~11M tokens) | Yes |
| Per-task cap | 100K tokens | Yes, per task or global |
| Local claw-code-parity work | Unlimited (local GPT-OSS 20B) | N/A |

**DeepSeek V3.2 rates:** $0.26/MTok input, $0.38/MTok output (~$0.30/MTok average).
**Fallback:** OpenRouter (5.5% markup) if DeepSeek has extended outage.

**When approaching daily limit:** Ada warns Daniel proactively.
**When monthly budget exceeded:** Band 3 escalation. Ada asks before spending more.

### Rate Limits

| Limit | Value | Type |
|-------|-------|------|
| Retries per task | 3 | Hard cap |
| Agent dispatches per hour | 5 | Soft cap (Ada warns) |
| Concurrent background tasks | 10 | Hard cap |
| Dispatch cooldown (same agent) | 30 seconds | Soft (override with urgency: high) |
| Subagent spawns per hour | 3 | Soft cap |

### Budget Class Routing

| Class | Meaning | Routing |
|-------|---------|---------|
| `free` | No cloud tokens | Ada local only (claw-code-parity + GPT-OSS 20B) |
| `cheap` | Minimal cloud | Short Arch consultation |
| `normal` | Standard | Arch and/or Forge, within per-task budget |
| `expensive` | Heavy cloud | Extended Arch/Forge work, requires daily budget headroom |

## Execution Journal

Because Ada operates with high autonomy, observability replaces permission gates.

The execution journal is a machine-readable append-only log of everything Ada does. Stored in PostgreSQL alongside tasks, notes, episodes, and memories.

### Journal Entry Schema

```json
{
  "entry_id": "jrn_20260402_001",
  "timestamp": "2026-04-02T10:30:15Z",
  "action_type": "dispatch | validate | retry | file_edit | command | note | state_change | budget_spend | escalation",
  "action_summary": "Dispatched task tsk_20260402_001 to ARCH",
  "task_id": "tsk_20260402_001",
  "agent": "ARCH",
  "band": 2,
  "budget_impact": {
    "class": "normal",
    "estimated_tokens": 5000
  },
  "rollback_hint": "Task can be cancelled via task_update",
  "state_before": "PROCESSING_FAST",
  "state_after": "DISPATCHING",
  "details": {}
}
```

### What Gets Journaled

| Action | Band | Always Logged |
|--------|------|--------------|
| Speech output | 1 | Summary only (not full text) |
| Note operations | 1 | Yes |
| Memory operations | 1 | Yes |
| File reads | 1 | Path only |
| File edits/writes | 2 | Full diff |
| Command execution | 2 | Command + exit code |
| Agent dispatches | 2 | Full task packet |
| Validation results | 2 | Gate results |
| Retries | 2 | Reason + attempt number |
| Config changes | 2 | Before/after |
| Escalations | 3 | Reason + user response |
| Budget spend | 2 | Token count + running total |

### Journal Queries

Ada can query her own journal:
- "What did I change today?" → filter by date, action_type = file_edit | command
- "How much did I spend?" → sum budget_impact for date range
- "What's pending?" → tasks with status in dispatched | awaiting_result | validating
- "Show retry history for task X" → filter by task_id, action_type = retry

Daniel can ask Ada:
- "What have you been doing?" → Ada summarizes recent journal entries
- "What changed in [project]?" → Ada filters journal by relevant task/file paths
- "How much cloud budget is left?" → Ada computes from journal + config

## Decision Gates (when Ada MUST escalate)

These are the ONLY situations where Ada stops and asks. Everything else is autonomous.

### A. Major Architecture Choices
Not every implementation detail. Only decisions that significantly shape the system.
- Framework switch
- Local-first vs cloud-heavy shift
- Database/storage architecture changes
- Agent structure changes
- Replacing core model/provider
- Major repo/rewrite direction

**Rule:** If the decision meaningfully changes the long-term system path, Ada escalates.

### B. Irreversible or Hard-to-Undo Actions
- Deleting important data or repos
- Destructive migrations
- Pushing to production/live environments
- Wiping environments
- Replacing working infrastructure with uncertain new setup
- Spending beyond budget threshold

**Rule:** If rollback is uncertain, incomplete, or expensive, Ada escalates once.

### C. Budget Threshold Crossings
Not every token call. Only meaningful spend shifts.
- Heavy Claude usage beyond daily budget
- Repeated failed loops consuming cloud tokens
- Expensive research runs
- Model/provider upgrades with cost implications

**Rule:** Ada can spend within defined operating budget. Crossing threshold = escalate.

### D. Ambiguous Strategic Pivots
- Ada detects priorities have changed
- Two reasonable interpretations of intent exist
- Continuing execution would lock into the wrong direction

**Rule:** If intent is strategically ambiguous, Ada asks for guidance.

### E. Conflicting High-Level Recommendations
- Arch recommends one path, Forge reveals execution reality that pushes another
- Cloud budget conflicts with desired speed
- Local model limits conflict with expectations

**Rule:** If specialists disagree in a way that changes direction, Ada summarizes and escalates.

**Everything else: Ada handles autonomously, logs to journal, reports when relevant.**

## Proactive Behaviors (not just allowed — core)

Ada should actively:
- Interrupt lightly with useful updates
- Ask for steering when direction is unclear
- Surface decisions at the right moment
- Proactively summarize progress
- Remind about drift or conflicts
- Flag when budget is being consumed faster than expected
- Notice when a task is stale and ask if it should be cancelled
- Report background work completion at natural conversation breaks
