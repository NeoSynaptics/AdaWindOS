"""
UltraPlan prompt templates — structured multi-pass planning.

Three passes inspired by Anthropic's ULTRAPLAN + Aristotle first-principles concept:
  Pass 1 (Decompose): Break problem into components, identify assumptions, map dependencies
  Pass 2 (Critique):  Challenge each component — what's wrong, what's missing, what's risky
  Pass 3 (Synthesize): Rebuild plan incorporating critique, produce actionable output

Optional domain-specific templates overlay on the base passes.

TODO: Implement template loading from ~/.ada/ultraplan_templates/ for custom overrides.
"""

# --- Base planning passes (domain-agnostic) ---

PASS_1_DECOMPOSE = """You are a senior systems architect doing deep planning work.
You have unlimited time. Be thorough, not fast.

TASK TO PLAN:
{brief}

DOMAIN CONTEXT:
{domain_context}

Produce a structured decomposition:

1. PROBLEM STATEMENT — Restate the task precisely. What exactly needs to be built/solved?

2. ASSUMPTIONS — List every assumption embedded in the brief.
   For each: is it validated, reasonable, or risky?

3. COMPONENTS — Break into discrete pieces of work.
   For each component:
   - What it does
   - What it depends on
   - What could go wrong
   - Estimated complexity (trivial / moderate / hard / research-needed)

4. DEPENDENCIES — Directed graph of what blocks what.
   What's the critical path?

5. UNKNOWNS — What do we NOT know that would change the plan?
   What experiments or spikes would reduce uncertainty?

6. FIRST DRAFT SEQUENCE — Ordered steps to execute.
   Mark which can parallelize.

Be exhaustive. This plan will be critiqued in the next pass."""


PASS_2_CRITIQUE = """You are reviewing a technical plan. Your job is to break it.
Find every weakness, gap, false assumption, and risk.

ORIGINAL TASK:
{brief}

PLAN TO CRITIQUE:
{previous_output}

For each section of the plan, answer:

1. ASSUMPTION CHALLENGES
   - Which assumptions are actually wrong or unvalidated?
   - What would happen if each assumption fails?

2. MISSING COMPONENTS
   - What was left out? What was oversimplified?
   - What edge cases weren't considered?

3. DEPENDENCY RISKS
   - Where could the critical path break?
   - Are there circular dependencies or hidden coupling?

4. COMPLEXITY UNDERESTIMATES
   - What was called "trivial" but is actually hard?
   - What integration points are glossed over?

5. ALTERNATIVE APPROACHES
   - Is there a fundamentally different way to solve this?
   - Would a different decomposition be better?

6. SHOWSTOPPERS
   - Is there anything that makes this plan infeasible?
   - What's the single biggest risk?

Be harsh. The goal is to catch problems now, not during implementation."""


PASS_3_SYNTHESIZE = """You are producing the final plan, incorporating critique feedback.

ORIGINAL TASK:
{brief}

INITIAL PLAN:
{pass_1_output}

CRITIQUE:
{pass_2_output}

Produce the FINAL PLAN:

## Summary
One paragraph: what we're building and why this approach.

## Revised Components
For each component (updated from critique):
- **What**: description
- **Why this approach**: reasoning, alternatives considered
- **Depends on**: prerequisites
- **Risk**: what could go wrong + mitigation
- **Estimate**: complexity + rough effort

## Execution Sequence
Ordered steps. Mark parallelizable work. Mark decision points.

## Key Decisions Made
Choices that constrain future work. Document the tradeoff.

## Open Questions
What still needs human input before execution starts.

## Validation Criteria
How do we know the implementation is correct?

This plan will be handed to an implementation agent. Make it unambiguous."""


# --- Domain-specific context overlays ---

DOMAIN_CONTEXT = {
    "sportwave": """Project: SportWave — mmWave + camera fusion for sports speed analysis.
Hardware: LD2451 sensor, RTX 4070. Stack: Expo app, FastAPI, Tauri dashboard.
Key constraint: real-time processing, sensor fusion accuracy.""",

    "blackdirt": """Project: BlackDirt — 3D swarm RTS with gesture commands.
Architecture: Group Brain (strategy) + Couzin School (movement).
Key constraint: 500+ dots at 60fps, emergent tactical behavior.""",

    "adaos": """Project: AdaOS — voice-first AI operating system.
Stack: Pipecat, Qwen 14B, Ollama, SQLite, Kokoro TTS.
Key constraint: <1s voice latency, local-first, high autonomy.""",

    "legal": """Context: Baratzategui legal case — finca expropriation.
Key constraint: legal accuracy, document cross-referencing, timeline precision.""",

    "general": """General planning task. No specific project context.""",
}


# --- Aristotle mode (first-principles overlay) ---
# Can be prepended to Pass 1 for deeper decomposition

ARISTOTLE_PREAMBLE = """Before decomposing this task, apply first-principles reasoning:

1. What is the ACTUAL problem, stripped of all conventional framing?
2. What do we know to be TRUE (not assumed, not conventional — proven)?
3. What constraints are REAL (physics, hardware, time) vs ARTIFICIAL (habit, convention)?
4. Starting from only the proven truths and real constraints,
   what is the simplest possible solution?
5. Only THEN layer on practical considerations (existing code, team, tools).

This prevents inheriting bad assumptions from how things are "usually done"."""


# TODO: Template versioning — track which template version produced each plan
# TODO: Custom templates from ~/.ada/ultraplan_templates/*.md
# TODO: Template selection heuristics — when to use Aristotle preamble automatically
