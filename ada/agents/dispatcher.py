"""Agent dispatcher — runs LangGraph worker graphs for Arch/Forge tasks.

One framework, one surface. LangGraph provides:
- Stateful graph-based workflows with cycles
- Built-in checkpointing (crash-safe)
- Tool calling with any LLM
- Streaming output for live progress

FORGE operates on REAL repositories (inspired by claw-code patterns):
- Workspace = actual repo path, not a temp dir
- Git context (status, diff, recent commits) injected into every prompt
- File tools (read, write, edit, grep, tree) operate on the real filesystem
- Coding model is local Qwen 32B via APU gateway (Ollama)
"""

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context window budget management
# ---------------------------------------------------------------------------

# Qwen 32B supports up to 131K but Ollama default is ~32K.
# We set num_ctx explicitly and manage our budget around it.
CONTEXT_WINDOW_TOKENS = 32_768      # conservative default, overridden by env
OUTPUT_RESERVE_TOKENS = 4_096       # reserved for model output per call
CHARS_PER_TOKEN = 3.5               # Qwen tokenizer is ~3.2-3.8 chars/tok

# Budget thresholds (fraction of usable input budget)
SUMMARIZE_THRESHOLD = 0.65          # summarize old messages at 65% usage
HARD_CAP_THRESHOLD = 0.85           # stop tool loop at 85% usage

# Tool output caps (chars)
READ_FILE_MAX_LINES = 200           # default max lines per read_file
READ_FILE_HARD_CAP_CHARS = 6_000    # absolute cap on read_file output
RUN_COMMAND_STDOUT_CAP = 1_500      # stdout cap in chars
RUN_COMMAND_STDERR_CAP = 500        # stderr cap in chars
GREP_MAX_LINES = 40                 # max grep result lines


def _estimate_tokens(text: str) -> int:
    """Rough token count from char length."""
    return int(len(text) / CHARS_PER_TOKEN)


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens across a message list."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if content:
            total += _estimate_tokens(content)
        # tool_calls in assistant messages
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            fn = tc.get("function", {})
            total += _estimate_tokens(fn.get("name", "") + fn.get("arguments", ""))
    return total


def _usable_input_budget() -> int:
    """Max tokens available for input (context window minus output reserve)."""
    ctx = int(os.environ.get("FORGE_CONTEXT_TOKENS", CONTEXT_WINDOW_TOKENS))
    return ctx - OUTPUT_RESERVE_TOKENS


def _summarize_old_messages(messages: list[dict], keep_last: int = 6) -> list[dict]:
    """Compress older messages into a summary, keeping the system prompt + recent turns.

    Strategy: keep messages[0] (system) + messages[1] (initial user) + summary + last N messages.
    """
    if len(messages) <= keep_last + 3:
        return messages  # nothing worth summarizing

    system = messages[0]
    initial_user = messages[1]
    old_section = messages[2:-keep_last]
    recent = messages[-keep_last:]

    # Build compact summary of old tool interactions
    summary_parts: list[str] = []
    files_read: set[str] = set()
    files_written: set[str] = set()
    commands_run: list[str] = []

    for msg in old_section:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                if name == "read_file":
                    files_read.add(args.get("path", "?"))
                elif name in ("write_file", "edit_file"):
                    files_written.add(args.get("path", "?"))
                elif name == "run_command":
                    commands_run.append(args.get("command", "?")[:80])
        elif role == "tool" and content:
            # Keep only errors from old tool results
            if content.startswith("ERROR"):
                summary_parts.append(f"  Tool error: {content[:150]}")

    summary_lines = ["[CONTEXT SUMMARY — older tool interactions compressed]"]
    if files_read:
        summary_lines.append(f"Files read: {', '.join(sorted(files_read))}")
    if files_written:
        summary_lines.append(f"Files modified: {', '.join(sorted(files_written))}")
    if commands_run:
        summary_lines.append(f"Commands run: {'; '.join(commands_run[:5])}")
    if summary_parts:
        summary_lines.extend(summary_parts[:5])

    summary_msg = {"role": "user", "content": "\n".join(summary_lines)}

    return [system, initial_user, summary_msg] + recent


def _truncate_output(text: str, max_chars: int) -> str:
    """Truncate text with a notice if it exceeds max_chars."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + f"\n... [truncated {len(text) - max_chars} chars] ...\n" + text[-half:]


# ---------------------------------------------------------------------------
# Repo context helpers (inspired by claw-code prompt.rs)
# ---------------------------------------------------------------------------

def _git_context(repo_path: str, max_diff_lines: int = 200) -> str:
    """Gather git status, recent commits, and staged/unstaged diff from a real repo."""
    if not repo_path or not Path(repo_path).joinpath(".git").exists():
        return "(not a git repository)"

    parts: list[str] = []

    def _run(cmd: list[str]) -> str:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, cwd=repo_path)
            return r.stdout.strip()
        except Exception:
            return ""

    # Branch + status
    status = _run(["git", "status", "--short", "--branch"])
    if status:
        parts.append(f"## git status\n```\n{status}\n```")

    # Recent commits
    log_out = _run(["git", "log", "--oneline", "-n", "10"])
    if log_out:
        parts.append(f"## recent commits\n```\n{log_out}\n```")

    # Unstaged diff (truncated)
    diff = _run(["git", "diff", "--stat"])
    if diff:
        parts.append(f"## unstaged changes (stat)\n```\n{diff}\n```")

    # Staged diff
    staged = _run(["git", "diff", "--cached", "--stat"])
    if staged:
        parts.append(f"## staged changes (stat)\n```\n{staged}\n```")

    return "\n\n".join(parts) if parts else "(clean working tree)"


def _file_tree(repo_path: str, max_depth: int = 3, max_files: int = 120) -> str:
    """Build a compact file tree of the repo, respecting .gitignore."""
    if not repo_path:
        return ""
    try:
        r = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, timeout=10, cwd=repo_path,
        )
        if r.returncode != 0:
            return ""
        files = r.stdout.strip().splitlines()[:max_files]
        if not files:
            return "(empty repo)"

        # Build compact tree
        tree_lines: list[str] = []
        for f in sorted(files):
            depth = f.count("/")
            if depth <= max_depth:
                tree_lines.append(f)
        suffix = f"\n... and {len(files) - len(tree_lines)} more files" if len(tree_lines) < len(files) else ""
        return "```\n" + "\n".join(tree_lines) + suffix + "\n```"
    except Exception:
        return ""


def _read_instruction_files(repo_path: str) -> str:
    """Walk ancestor dirs looking for CLAUDE.md / project instruction files."""
    if not repo_path:
        return ""
    candidates = ["CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md", "ADA.md"]
    found: list[str] = []
    cursor = Path(repo_path)
    visited = set()
    while cursor != cursor.parent:
        if str(cursor) in visited:
            break
        visited.add(str(cursor))
        for name in candidates:
            p = cursor / name
            if p.is_file():
                try:
                    content = p.read_text()[:2000]  # cap instruction files to save context
                    found.append(f"### {p.relative_to(repo_path) if str(p).startswith(repo_path) else p}\n{content}")
                except Exception:
                    pass
        cursor = cursor.parent
    return "\n\n".join(found)


# ---------------------------------------------------------------------------
# File tools — the model calls these via structured JSON tool_calls
# ---------------------------------------------------------------------------

FORGE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root"},
                    "offset": {"type": "integer", "description": "Start line (0-indexed, optional)"},
                    "limit": {"type": "integer", "description": "Max lines to read (optional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file. Use for new files or complete rewrites.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root"},
                    "content": {"type": "string", "description": "Full file content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a specific string in a file. old_string must be unique in the file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root"},
                    "old_string": {"type": "string", "description": "Exact text to find and replace"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "Search file contents with regex pattern. Returns matching lines with context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                    "glob": {"type": "string", "description": "File glob filter (e.g. '*.py')"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files matching a glob pattern in the repo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. 'src/**/*.py')"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command in the repo directory. Use for running tests, linters, type checks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)"},
                },
                "required": ["command"],
            },
        },
    },
]


def _execute_tool(tool_name: str, args: dict, workspace: str) -> str:
    """Execute a file tool against the real workspace."""
    ws = Path(workspace)

    if tool_name == "read_file":
        fpath = ws / args["path"]
        if not fpath.exists():
            return f"ERROR: File not found: {args['path']}"
        if not str(fpath.resolve()).startswith(str(ws.resolve())):
            return "ERROR: Path escapes workspace"
        try:
            lines = fpath.read_text().splitlines()
            offset = args.get("offset", 0)
            limit = min(args.get("limit", READ_FILE_MAX_LINES), READ_FILE_MAX_LINES)
            selected = lines[offset:offset + limit]
            numbered = [f"{i + offset + 1}\t{line}" for i, line in enumerate(selected)]
            result = "\n".join(numbered)
            if len(lines) > offset + limit:
                result += f"\n... ({len(lines) - offset - limit} more lines, use offset to read further)"
            return _truncate_output(result, READ_FILE_HARD_CAP_CHARS)
        except Exception as e:
            return f"ERROR: {e}"

    elif tool_name == "write_file":
        fpath = ws / args["path"]
        if not str(fpath.resolve()).startswith(str(ws.resolve())):
            return "ERROR: Path escapes workspace"
        try:
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(args["content"])
            return f"OK: wrote {len(args['content'])} bytes to {args['path']}"
        except Exception as e:
            return f"ERROR: {e}"

    elif tool_name == "edit_file":
        fpath = ws / args["path"]
        if not fpath.exists():
            return f"ERROR: File not found: {args['path']}"
        if not str(fpath.resolve()).startswith(str(ws.resolve())):
            return "ERROR: Path escapes workspace"
        try:
            content = fpath.read_text()
            old = args["old_string"]
            count = content.count(old)
            if count == 0:
                return f"ERROR: old_string not found in {args['path']}"
            if count > 1:
                return f"ERROR: old_string matches {count} times — must be unique. Add more context."
            new_content = content.replace(old, args["new_string"], 1)
            fpath.write_text(new_content)
            return f"OK: replaced in {args['path']}"
        except Exception as e:
            return f"ERROR: {e}"

    elif tool_name == "grep_search":
        try:
            cmd = ["grep", "-rn", "--include", args.get("glob", "*"), "-E", args["pattern"], "."]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, cwd=workspace)
            output = r.stdout.strip()
            if not output:
                return "No matches found."
            lines = output.splitlines()
            if len(lines) > GREP_MAX_LINES:
                return "\n".join(lines[:GREP_MAX_LINES]) + f"\n... ({len(lines)} total matches, narrow your pattern)"
            return output
        except Exception as e:
            return f"ERROR: {e}"

    elif tool_name == "list_files":
        import glob as globmod
        try:
            matches = sorted(globmod.glob(args["pattern"], root_dir=workspace, recursive=True))
            if not matches:
                return "No files matched."
            if len(matches) > 100:
                return "\n".join(matches[:100]) + f"\n... ({len(matches)} total)"
            return "\n".join(matches)
        except Exception as e:
            return f"ERROR: {e}"

    elif tool_name == "run_command":
        try:
            timeout = min(args.get("timeout", 60), 90)  # hard cap 90s
            import shlex
            r = subprocess.run(
                shlex.split(args["command"]),
                capture_output=True, text=True, timeout=timeout, cwd=workspace,
            )
            output = ""
            if r.stdout:
                output += _truncate_output(r.stdout, RUN_COMMAND_STDOUT_CAP)
            if r.stderr:
                output += f"\nSTDERR:\n{_truncate_output(r.stderr, RUN_COMMAND_STDERR_CAP)}"
            output += f"\n(exit code: {r.returncode})"
            return output.strip()
        except subprocess.TimeoutExpired:
            return f"ERROR: Command timed out after {timeout}s"
        except Exception as e:
            return f"ERROR: {e}"

    return f"ERROR: Unknown tool {tool_name}"


# ---------------------------------------------------------------------------
# Agent soul prompts
# ---------------------------------------------------------------------------

ARCH_SOUL = (
    "You are Arch, a system designer and tradeoff analyst.\n"
    "You produce architecture specs, feasibility analyses, and design documents.\n"
    "You do NOT write implementation code. You design and analyze.\n"
    "Be thorough. Consider tradeoffs. Flag risks. Propose alternatives.\n"
    "Output as structured markdown."
)

FORGE_SOUL = (
    "You are Forge, an implementation specialist working in a REAL repository.\n"
    "You have tools to read, write, edit, search, and run commands in the actual codebase.\n\n"
    "# Rules\n"
    "- ALWAYS read existing files before modifying them. Understand the code first.\n"
    "- Follow existing conventions in the repo (naming, structure, imports).\n"
    "- Use edit_file for surgical changes. Use write_file only for new files.\n"
    "- Run tests after making changes. Fix failures before finishing.\n"
    "- Work in the nested folder structure as it exists. Create dirs via write_file.\n"
    "- Do NOT create temp files or scratch pads — work directly in the repo.\n"
    "- If something is unclear, read more code. grep_search is your friend.\n"
    "- Keep changes minimal and focused on the task.\n\n"
    "# Do NOT\n"
    "- Do NOT write code in chat. Use the tools.\n"
    "- Do NOT guess at APIs or imports. Read the source.\n"
    "- Do NOT overwrite files you haven't read.\n"
    "- Do NOT make changes outside the scope of the task.\n"
)


# ---------------------------------------------------------------------------
# ARCH graph (unchanged — research → analyze → synthesize)
# ---------------------------------------------------------------------------

def _build_arch_graph():
    """Build the ARCH worker graph: research → analyze → synthesize → deliver."""
    from langgraph.graph import StateGraph, END
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage
    from typing import TypedDict, Annotated
    import operator

    class ArchState(TypedDict):
        messages: Annotated[list, operator.add]
        task_packet: dict
        research: str
        analysis: str
        synthesis: str
        review: str
        artifacts: list

    def research(state: ArchState) -> dict:
        try:
            llm = _get_worker_llm("arch")
            task = state["task_packet"]
            resp = llm.invoke([
                SystemMessage(content=ARCH_SOUL),
                HumanMessage(content=(
                    f"TASK: {task.get('objective', '')}\n"
                    f"CONTEXT: {task.get('context', '')}\n\n"
                    "Phase 1 — RESEARCH: Gather relevant information, prior art, constraints. "
                    "List what you know and what you need to find out."
                )),
            ])
            return {"research": resp.content, "messages": [resp]}
        except Exception as e:
            log.error(f"ARCH research node failed: {e}")
            return {"research": f"[RESEARCH FAILED: {e}]", "messages": []}

    def analyze(state: ArchState) -> dict:
        try:
            llm = _get_worker_llm("arch")
            resp = llm.invoke([
                SystemMessage(content=ARCH_SOUL),
                HumanMessage(content=(
                    f"Research findings:\n{state['research']}\n\n"
                    "Phase 2 — ANALYZE: Identify tradeoffs, risks, dependencies. "
                    "Compare approaches. Flag unknowns."
                )),
            ])
            return {"analysis": resp.content, "messages": [resp]}
        except Exception as e:
            log.error(f"ARCH analyze node failed: {e}")
            return {"analysis": f"[ANALYSIS FAILED: {e}]", "messages": []}

    def synthesize(state: ArchState) -> dict:
        try:
            llm = _get_worker_llm("arch")
            task = state["task_packet"]
            resp = llm.invoke([
                SystemMessage(content=ARCH_SOUL),
                HumanMessage(content=(
                    f"Original task: {task.get('objective', '')}\n"
                    f"Research: {state['research']}\n"
                    f"Analysis: {state['analysis']}\n\n"
                    "Phase 3 — SYNTHESIZE: Produce a final recommendation with clear rationale. "
                    "Include: summary, recommended approach, key decisions, open questions."
                )),
            ])
            return {
                "synthesis": resp.content,
                "artifacts": [{"type": "spec", "content": resp.content}],
                "messages": [resp],
            }
        except Exception as e:
            log.error(f"ARCH synthesize node failed: {e}")
            return {"synthesis": f"[SYNTHESIS FAILED: {e}]", "artifacts": [], "messages": []}

    graph = StateGraph(ArchState)
    graph.add_node("research", research)
    graph.add_node("analyze", analyze)
    graph.add_node("synthesize", synthesize)
    graph.set_entry_point("research")
    graph.add_edge("research", "analyze")
    graph.add_edge("analyze", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile()


# ---------------------------------------------------------------------------
# FORGE graph — repo-grounded agentic coding with tool loop
# ---------------------------------------------------------------------------

def _build_forge_graph():
    """Build the FORGE worker graph: plan → agent_loop (tool cycle) → deliver.

    Unlike the old version, this operates on the REAL repo:
    - Workspace is the actual repo path (no temp dirs)
    - Git context + file tree injected into system prompt
    - Model has file tools to read/write/edit/search the codebase
    - Tests run against the actual project
    """
    from langgraph.graph import StateGraph, END
    from typing import TypedDict, Annotated
    import operator

    class ForgeState(TypedDict):
        messages: Annotated[list, operator.add]
        task_packet: dict
        workspace: str              # REAL repo path
        repo_context: str           # git status + tree + instructions
        plan: str
        files_changed: list         # [{path, action}] — audit trail
        iteration: int              # tool loop iterations
        max_iterations: int         # safety cap
        test_passed: bool
        artifacts: list

    def plan(state: ForgeState) -> dict:
        """Read the repo, build context, create an implementation plan."""
        try:
            workspace = state.get("workspace", "")
            task = state["task_packet"]

            if not workspace or not Path(workspace).is_dir():
                return {
                    "plan": "[PLAN FAILED: no valid workspace path provided]",
                    "repo_context": "",
                    "messages": [],
                }

            # Build repo context (claw-code pattern: CWD as source of truth)
            git_ctx = _git_context(workspace)
            tree = _file_tree(workspace)
            instructions = _read_instruction_files(workspace)

            repo_context = (
                f"# Repository: {Path(workspace).name}\n"
                f"Path: {workspace}\n\n"
                f"## File tree\n{tree}\n\n"
                f"{git_ctx}\n\n"
            )
            if instructions:
                repo_context += f"## Project instructions\n{instructions}\n\n"

            # Ask model to plan
            llm = _get_worker_llm("forge")
            system = FORGE_SOUL + f"\n\n# Repository context\n\n{repo_context}"

            resp = llm.invoke([
                {"role": "system", "content": system},
                {"role": "user", "content": (
                    f"TASK: {task.get('objective', '')}\n"
                    f"CONTEXT: {task.get('context', '')}\n"
                    f"CONSTRAINTS: {json.dumps(task.get('constraints', []))}\n\n"
                    "Create a plan. What files need to be read, created, or modified? "
                    "What's the implementation order? What tests should run after?"
                )},
            ])
            content = resp.content if hasattr(resp, "content") else str(resp)
            return {
                "plan": content,
                "repo_context": repo_context,
                "messages": [{"role": "assistant", "content": content}],
            }
        except Exception as e:
            log.error(f"FORGE plan node failed: {e}")
            return {"plan": f"[PLAN FAILED: {e}]", "repo_context": "", "messages": []}

    def agent_loop(state: ForgeState) -> dict:
        """Agentic tool-use loop with context budget management.

        The model gets file tools (read, write, edit, grep, list, run_command).
        It works iteratively until done, hits max_iterations, or nears context limit.

        Context management strategy:
        - Track token usage after each iteration
        - At SUMMARIZE_THRESHOLD (65%): compress older messages into a summary
        - At HARD_CAP_THRESHOLD (85%): force-stop the loop to avoid overflow
        - Tool outputs are individually capped (read_file, run_command, grep)
        """
        try:
            workspace = state.get("workspace", "")
            task = state["task_packet"]
            repo_context = state.get("repo_context", "")
            plan = state.get("plan", "")
            max_iters = state.get("max_iterations", 20)

            llm = _get_worker_llm("forge")
            system = FORGE_SOUL + f"\n\n# Repository context\n\n{repo_context}"

            # Build conversation with plan context
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": (
                    f"TASK: {task.get('objective', '')}\n"
                    f"CONTEXT: {task.get('context', '')}\n\n"
                    f"PLAN:\n{plan}\n\n"
                    "Now implement the plan. Use your tools to read, write, edit files and "
                    "run tests. When you're done, respond with DONE and a summary of changes."
                )},
            ]

            # Tool definitions also consume tokens (estimated once)
            tools_token_overhead = _estimate_tokens(json.dumps(FORGE_TOOLS))
            input_budget = _usable_input_budget()
            summarize_at = int(input_budget * SUMMARIZE_THRESHOLD)
            hard_cap_at = int(input_budget * HARD_CAP_THRESHOLD)
            summarization_count = 0

            files_changed: list[dict] = list(state.get("files_changed", []))
            iteration = 0

            while iteration < max_iters:
                iteration += 1

                # --- Context budget check ---
                current_tokens = _estimate_messages_tokens(messages) + tools_token_overhead

                if current_tokens >= hard_cap_at:
                    log.warning(
                        f"FORGE context hard cap reached: {current_tokens}/{input_budget} tokens "
                        f"at iteration {iteration}. Stopping loop."
                    )
                    messages.append({"role": "assistant", "content": (
                        "DONE (context budget reached). Summary of changes so far: "
                        f"modified {len(files_changed)} files: {[fc['path'] for fc in files_changed]}"
                    )})
                    break

                if current_tokens >= summarize_at and summarization_count < 3:
                    old_len = len(messages)
                    old_tokens = current_tokens
                    messages = _summarize_old_messages(messages, keep_last=6)
                    new_tokens = _estimate_messages_tokens(messages) + tools_token_overhead
                    summarization_count += 1
                    log.info(
                        f"FORGE context summarized: {old_len}→{len(messages)} messages, "
                        f"~{old_tokens}→{new_tokens} tokens (pass {summarization_count})"
                    )

                # --- LLM call with tools ---
                resp = llm.invoke(messages, tools=FORGE_TOOLS)

                # Check if model wants to call tools
                tool_calls = getattr(resp, "tool_calls", None) or []

                if not tool_calls:
                    # Model is done — no more tool calls
                    content = resp.content if hasattr(resp, "content") else str(resp)
                    messages.append({"role": "assistant", "content": content})
                    break

                # Process each tool call
                messages.append({"role": "assistant", "content": "", "tool_calls": [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])}}
                    for tc in tool_calls
                ]})

                for tc in tool_calls:
                    tool_name = tc["name"]
                    tool_args = tc["args"]
                    log.info(f"FORGE tool: {tool_name}({json.dumps(tool_args)[:200]})")

                    result = _execute_tool(tool_name, tool_args, workspace)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })

                    # Track file changes
                    if tool_name in ("write_file", "edit_file") and not result.startswith("ERROR"):
                        files_changed.append({
                            "path": tool_args.get("path", ""),
                            "action": "write" if tool_name == "write_file" else "edit",
                        })

            final_tokens = _estimate_messages_tokens(messages) + tools_token_overhead
            log.info(
                f"FORGE agent loop: {iteration} iterations, {len(files_changed)} files changed, "
                f"~{final_tokens}/{input_budget} tokens used ({summarization_count} summarizations)"
            )

            return {
                "files_changed": files_changed,
                "iteration": iteration,
                "messages": messages[2:],  # skip system + initial user
            }
        except Exception as e:
            log.error(f"FORGE agent loop failed: {e}", exc_info=True)
            return {"files_changed": [], "iteration": 0, "messages": []}

    def verify(state: ForgeState) -> dict:
        """Run tests/checks against the actual repo after changes."""
        try:
            workspace = state.get("workspace", "")
            files_changed = state.get("files_changed", [])

            if not workspace:
                return {"test_passed": False, "artifacts": []}

            results: list[str] = []
            all_passed = True

            # Syntax check all changed Python files
            for fc in files_changed:
                fpath = Path(workspace) / fc["path"]
                if fpath.suffix == ".py" and fpath.exists():
                    try:
                        compile(fpath.read_text(), str(fpath), "exec")
                        results.append(f"SYNTAX OK: {fc['path']}")
                    except SyntaxError as e:
                        results.append(f"SYNTAX ERROR: {fc['path']}: {e}")
                        all_passed = False

            # Run pytest if available
            try:
                proc = subprocess.run(
                    ["python", "-m", "pytest", "--tb=short", "-q"],
                    capture_output=True, text=True, timeout=120, cwd=workspace,
                )
                if proc.returncode == 0:
                    results.append(f"PYTEST: PASSED\n{proc.stdout[-500:]}")
                else:
                    results.append(f"PYTEST: FAILED\n{proc.stdout[-500:]}\n{proc.stderr[-500:]}")
                    all_passed = False
            except FileNotFoundError:
                results.append("PYTEST: not available — cannot verify")
                all_passed = False
            except subprocess.TimeoutExpired:
                results.append("PYTEST: timed out (120s)")
                all_passed = False

            test_summary = "\n".join(results)
            log.info(f"FORGE verify: passed={all_passed}\n{test_summary}")

            artifacts = []
            if all_passed and files_changed:
                # Collect a diff of what was changed
                try:
                    diff = subprocess.run(
                        ["git", "diff"], capture_output=True, text=True,
                        timeout=10, cwd=workspace,
                    )
                    artifacts.append({
                        "type": "code",
                        "content": diff.stdout[:10000] if diff.stdout else "(no diff — files may be untracked)",
                        "files_changed": [fc["path"] for fc in files_changed],
                        "test_summary": test_summary,
                    })
                except Exception:
                    artifacts.append({
                        "type": "code",
                        "files_changed": [fc["path"] for fc in files_changed],
                        "test_summary": test_summary,
                    })

            return {
                "test_passed": all_passed,
                "artifacts": artifacts,
            }
        except Exception as e:
            log.error(f"FORGE verify failed: {e}")
            return {"test_passed": False, "artifacts": []}

    def should_retry(state: ForgeState) -> str:
        if state.get("test_passed") or state.get("artifacts"):
            return "deliver"
        if state.get("iteration", 0) >= state.get("max_iterations", 20):
            return "deliver"
        return "agent_loop"

    def deliver(state: ForgeState) -> dict:
        if not state.get("artifacts"):
            return {"artifacts": [{
                "type": "failed_code",
                "note": f"Tests did not pass after {state.get('iteration', 0)} iterations",
                "files_changed": [fc["path"] for fc in state.get("files_changed", [])],
            }]}
        return {}

    graph = StateGraph(ForgeState)
    graph.add_node("plan", plan)
    graph.add_node("agent_loop", agent_loop)
    graph.add_node("verify", verify)
    graph.add_node("deliver", deliver)
    graph.set_entry_point("plan")
    graph.add_edge("plan", "agent_loop")
    graph.add_edge("agent_loop", "verify")
    graph.add_conditional_edges("verify", should_retry, {"agent_loop": "agent_loop", "deliver": "deliver"})
    graph.add_edge("deliver", END)
    return graph.compile()


# ---------------------------------------------------------------------------
# Worker LLM factory — local Qwen via Ollama or cloud fallback
# ---------------------------------------------------------------------------

_worker_llms: dict[str, Any] = {}


def _get_worker_llm(agent_type: str = "forge"):
    """Get the worker LLM.

    - forge: local Qwen 32B via Ollama (coding tasks)
    - arch: local Qwen 32B via Ollama (also strong reasoning) or cloud fallback
    - fallback: DeepSeek V3.2 cloud API
    """
    global _worker_llms
    if agent_type in _worker_llms:
        return _worker_llms[agent_type]

    from langchain_openai import ChatOpenAI

    # Try local Ollama first (Qwen 32B for both forge and arch)
    ollama_base = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434/v1")
    local_model = os.environ.get("FORGE_MODEL", "qwen3:32b")

    if agent_type in ("forge", "arch"):
        try:
            ctx_size = int(os.environ.get("FORGE_CONTEXT_TOKENS", CONTEXT_WINDOW_TOKENS))
            llm = ChatOpenAI(
                model=local_model,
                openai_api_key="ollama",        # Ollama doesn't need a key
                openai_api_base=ollama_base,
                max_tokens=OUTPUT_RESERVE_TOKENS,
                temperature=0.1,
                timeout=300,                     # local model can be slower
                max_retries=1,
                model_kwargs={"num_ctx": ctx_size},  # explicit context window
            )
            _worker_llms[agent_type] = llm
            log.info(f"Worker LLM ({agent_type}): local {local_model} via {ollama_base}")
            return llm
        except Exception as e:
            log.warning(f"Local Ollama not available for {agent_type}: {e}, falling back to cloud")

    # Cloud fallback
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    api_base = os.environ.get("WORKER_API_BASE", "https://api.deepseek.com/v1")
    model = os.environ.get("WORKER_MODEL", "deepseek-chat")

    if not api_key:
        log.warning("DEEPSEEK_API_KEY not set — worker dispatches will fail")

    llm = ChatOpenAI(
        model=model,
        openai_api_key=api_key,
        openai_api_base=api_base,
        max_tokens=8192,
        temperature=0.1,
        timeout=120,
        max_retries=2,
    )
    _worker_llms[agent_type] = llm
    log.info(f"Worker LLM ({agent_type}): cloud {model} via {api_base}")
    return llm


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class AgentDispatcher:
    """Dispatches tasks to LangGraph worker graphs."""

    def __init__(self):
        self._arch_graph = None
        self._forge_graph = None

    def _get_arch_graph(self):
        if self._arch_graph is None:
            self._arch_graph = _build_arch_graph()
        return self._arch_graph

    def _get_forge_graph(self):
        if self._forge_graph is None:
            self._forge_graph = _build_forge_graph()
        return self._forge_graph

    async def dispatch(self, payload: dict) -> dict:
        """Dispatch a task to the appropriate LangGraph worker."""
        task_id = payload["task_id"]
        agent = payload.get("agent", "ARCH")

        log.info(f"Dispatching {task_id} to {agent} via LangGraph")

        try:
            graph = self._get_arch_graph() if agent == "ARCH" else self._get_forge_graph()

            initial_state = {
                "messages": [],
                "task_packet": payload,
                "artifacts": [],
            }

            if agent == "ARCH":
                initial_state.update({
                    "research": "", "analysis": "", "synthesis": "", "review": "",
                })
            else:
                # FORGE: workspace is the REAL repo path from the task packet
                workspace = payload.get("repo_path", payload.get("workspace", ""))
                if not workspace:
                    log.error(f"FORGE task {task_id} has no repo_path — cannot proceed")
                    return {
                        "task_id": task_id,
                        "status": "failed",
                        "error": "No repo_path provided. FORGE requires a real repository path.",
                        "artifacts": [],
                    }

                initial_state.update({
                    "workspace": workspace,
                    "repo_context": "",
                    "plan": "",
                    "files_changed": [],
                    "iteration": 0,
                    "max_iterations": 20,
                    "test_passed": False,
                })

            result = await graph.ainvoke(initial_state)

            artifacts = result.get("artifacts", [])
            if artifacts:
                log.info(f"LangGraph {agent} completed {task_id}: {len(artifacts)} artifacts")
                return {
                    "task_id": task_id,
                    "status": "completed",
                    "artifacts": artifacts,
                }
            else:
                log.warning(f"LangGraph {agent} produced no artifacts for {task_id}")
                return {
                    "task_id": task_id,
                    "status": "failed",
                    "error": "Worker produced no artifacts",
                    "artifacts": [],
                }

        except Exception as e:
            log.error(f"LangGraph execution failed for {task_id}: {e}", exc_info=True)
            return {
                "task_id": task_id,
                "status": "failed",
                "error": f"Worker crashed: {e}",
                "artifacts": [],
            }
