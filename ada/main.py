"""AdaWindOS entry point — cloud-first, no GPU required."""

import asyncio
import json
import logging
import sys
from datetime import datetime

from .config import AdaConfig
from .state import SystemState
from .memory.store import Store
from .journal.logger import Journal
from .executive.outbox import OutboxWorker
from .executive.task_manager import TaskManager
from .decision.classifier import classify_with_fallback
from .decision.resolver import ActionResolver, ActionPlan
from .decision.event import DecisionEvent, Source, Classification, Action, Execution, Meta
from .memory.models import OutboxEvent, Episode, Note
from .memory.context_builder import ContextBuilder
from .memory.consolidation import ConsolidationEngine
from .memory.embeddings import embed
from .agents.dispatcher import AgentDispatcher
from .noteboard.server import NoteboardServer
from .executive.validator import Validator
from .executive.critic import CriticService
from .apu.gateway import CloudGateway, APUInferenceError
from .tools.handlers import ToolHandlers
from .tools.registry import ToolRegistry
from .tools.core import register_core_tools, register_extended_tools
from .sentinel.probe import Probe
from .sentinel.report import ReportBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("ada")

CONSOLIDATION_INTERVAL_SEC = 1800  # 30 minutes


class Ada:
    def __init__(self, config: AdaConfig | None = None):
        self.config = config or AdaConfig()
        self.state = SystemState()
        self.store = Store(self.config.database)
        self.journal = Journal(self.store)
        self.task_manager = TaskManager(self.store, self.journal)
        self.outbox = OutboxWorker(self.store, self.journal)
        async def _embed_query(text: str) -> list[float]:
            return await embed(text, is_query=True)

        # Cloud gateway — replaces the APU gateway
        self.apu_gateway = CloudGateway(
            api_base=self.config.cloud.api_base,
            api_key=self.config.cloud.api_key,
            default_model=self.config.cloud.default_model,
        )

        self.context_builder = ContextBuilder(
            self.store, self.state, embed_fn=_embed_query,
            budget=self.config.context_budget,
        )
        self.consolidation = ConsolidationEngine(
            store=self.store,
            gateway=self.apu_gateway,
            model=self.config.models.control_model,
            embed_fn=embed,
        )
        self.agent_dispatcher = AgentDispatcher()
        self.noteboard = NoteboardServer(self.store)
        self.validator = Validator(
            ollama_base_url="",
            model=self.config.models.control_model,
            gateway=self.apu_gateway,
        )
        self.critic = CriticService(
            ollama_base_url="",
            model=self.config.models.control_model,
            candidate_count=self.config.tribe.candidate_count,
            tribe_enabled=self.config.tribe.enabled_background,
        )

        # Voice pipeline — only initialized if enabled
        self.voice_pipeline = None

        # Tool registry
        self.tool_handlers = ToolHandlers(
            store=self.store,
            gateway=self.apu_gateway,
            dispatcher=self.agent_dispatcher,
            validator=self.validator,
            noteboard=self.noteboard,
            voice_pipeline=None,
        )
        self.tool_registry = ToolRegistry()
        register_core_tools(self.tool_registry, self.tool_handlers)
        register_extended_tools(self.tool_registry, self.tool_handlers)

        # Sentinel — lightweight, no GPU needed
        self.sentinel_probe = Probe()
        self.sentinel_report_builder = ReportBuilder()
        self.sentinel_registry = None
        self.sentinel_gate = None
        self.sentinel_diagnostics = None

        self._sequence = 0
        self._session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._pending_deliveries: list[dict] = []
        self._last_note_exists: bool = False

    async def start(self) -> None:
        """Initialize all subsystems."""
        log.info("Ada starting...")
        await self.store.connect()
        await self.store.create_session(self._session_id)

        # Initialize APU — register model fleet and reconcile with Ollama
        if self.config.apu.enabled:
            await self._init_apu()

        # Start outbox worker in background
        asyncio.create_task(self.outbox.start())

        # Start consolidation loop in background
        asyncio.create_task(self._consolidation_loop())

        # Start noteboard WebSocket server
        asyncio.create_task(self.noteboard.start())

        # Register outbox deliverers
        self.outbox.register("agent.dispatch", self._deliver_agent_dispatch)
        self.outbox.register("noteboard.push", self._deliver_noteboard_push)
        self.outbox.register("delivery.queue", self._deliver_result_to_user)

        log.info(f"Ada ready (cloud mode). Session: {self._session_id}")

    async def _consolidation_loop(self) -> None:
        """Run memory consolidation every 30 minutes."""
        while True:
            await asyncio.sleep(CONSOLIDATION_INTERVAL_SEC)
            try:
                if not self.state.is_live_turn:
                    log.info("Running memory consolidation...")
                    stats = await self.consolidation.run()
                    if stats["facts_extracted"] > 0:
                        log.info(f"Consolidation: {stats}")
            except Exception as e:
                log.error(f"Consolidation failed: {e}")

    async def _learning_loop_disabled(self) -> None:
        """LoRA fine-tuning — disabled in cloud mode (no GPU).

        Kept as placeholder for future cloud fine-tuning integration.
        """
        return
        # Original code below kept for reference
        import datetime as dt
        while True:
            await asyncio.sleep(3600)
            try:
                hour = dt.datetime.now().hour
                if hour < 0 or hour >= 6:
                    continue
                if self.state.is_live_turn:
                    continue

                log.info("Starting overnight learning loop...")
                result = await run_learning_loop(
                    pool=self.store.pool,
                    min_quality=0.6,
                    min_pairs=20,
                    since_days=90,
                )
                log.info(f"Learning loop result: {result}")
            except Exception as e:
                log.error(f"Learning loop failed: {e}", exc_info=True)

    async def _init_apu(self) -> None:
        """Register model fleet with APU and reconcile with Ollama."""
        cfg = self.config
        try:
            await self.apu_monitor.start()

            # Register model fleet
            self.apu_registry.register(ModelCard(
                name="faster-whisper",
                tier=ModelTier.RESIDENT,
                vram_mb=cfg.apu.stt_vram_mb,
                ram_mb=0,
                preferred_gpu=GPU.GPU_1,
                location=ModelLocation.GPU_1,  # always loaded
                description="Whisper STT — always on GPU_1",
            ))

            self.apu_registry.register(ModelCard(
                name=cfg.models.control_model,
                tier=ModelTier.VOICE,
                vram_mb=cfg.apu.voice_model_vram_mb,
                ram_mb=cfg.apu.voice_model_vram_mb,
                preferred_gpu=GPU.GPU_0,
                description="Ada voice brain — classification + response",
            ))

            # Qwen 32B coding model — 18GB needs GPU_SPLIT (both GPUs) when voice is evicted
            if cfg.apu.coding_model:
                self.apu_registry.register(ModelCard(
                    name=cfg.apu.coding_model,
                    tier=ModelTier.CODING,
                    vram_mb=cfg.apu.coding_model_vram_mb,
                    ram_mb=cfg.apu.coding_model_vram_mb,
                    preferred_gpu=None,  # uses GPU_0 after voice evicted, spills to GPU_1 if needed
                    description=f"Coding model ({cfg.apu.coding_model}) — FORGE tasks",
                ))

            # Gemma 4 multimodal model — vision/image tasks, fits on GPU_0
            if cfg.apu.multimodal_model:
                self.apu_registry.register(ModelCard(
                    name=cfg.apu.multimodal_model,
                    tier=ModelTier.CODING,  # same tier as coding — evicts for voice
                    vram_mb=cfg.apu.multimodal_model_vram_mb,
                    ram_mb=cfg.apu.multimodal_model_vram_mb,
                    preferred_gpu=GPU.GPU_0,
                    description=f"Multimodal model ({cfg.apu.multimodal_model}) — vision tasks",
                ))

            self.apu_registry.register(ModelCard(
                name=cfg.models.plan_model,
                tier=ModelTier.PLANNING,
                vram_mb=cfg.apu.planning_model_vram_mb,
                ram_mb=cfg.apu.planning_model_vram_mb,
                preferred_gpu=None,  # needs both GPUs
                description="UltraPlan overnight — Qwen 72B",
            ))

            # Reconcile with actual Ollama state
            await self.apu_orchestrator.reconcile()

            # Ensure voice model is loaded
            result = await self.apu_orchestrator.ensure_loaded(cfg.models.control_model, gpu=GPU.GPU_0)
            if result.success:
                log.info(f"APU: voice model {cfg.models.control_model} ready on GPU_0")
            else:
                log.warning(f"APU: voice model load failed: {result.error} — will retry on first input")

            # Wire gateway into all subsystems that use LLM
            self.consolidation.gateway = self.apu_gateway
            self.validator.gateway = self.apu_gateway
            self.critic.gateway = self.apu_gateway
            self.tool_handlers.gateway = self.apu_gateway

            log.info("APU initialized")

        except Exception as e:
            log.error(f"APU initialization failed: {e}", exc_info=True)
            log.warning("APU disabled — falling back to direct Ollama calls")

    async def _apu_idle_monitor(self) -> None:
        """Monitor voice state and swap models when Daniel goes idle.

        Voice active → ensure voice model on GPU
        Voice idle for N seconds → swap to coding model
        Overnight window → swap to planning model
        """
        from .state import VoiceState
        cfg = self.config.apu
        idle_since: datetime | None = None

        while True:
            await asyncio.sleep(10)  # check every 10 seconds

            try:
                now = datetime.now()
                hour = now.hour

                # Overnight planning mode
                if cfg.overnight_start_hour > cfg.overnight_end_hour:
                    is_overnight = hour >= cfg.overnight_start_hour or hour < cfg.overnight_end_hour
                else:
                    is_overnight = cfg.overnight_start_hour <= hour < cfg.overnight_end_hour

                if is_overnight and self.state.voice == VoiceState.IDLE:
                    # Check if planning model is already loaded
                    planning_card = self.apu_registry.get(self.config.models.plan_model)
                    if planning_card and not planning_card.is_on_gpu:
                        log.info("APU: overnight window — transitioning to planning mode")
                        await self.apu_orchestrator.transition_to_planning_mode()
                    continue

                # Daytime voice/coding swap
                if self.state.is_live_turn:
                    # Daniel is talking — ensure voice model is loaded
                    idle_since = None
                    voice_card = self.apu_registry.get(self.config.models.control_model)
                    if voice_card and not voice_card.is_on_gpu:
                        log.info("APU: voice activity — transitioning to voice mode")
                        await self.apu_orchestrator.transition_to_voice_mode()

                elif self.state.voice == VoiceState.IDLE:
                    # Daniel is idle
                    if idle_since is None:
                        idle_since = now
                    elif (now - idle_since).total_seconds() >= cfg.idle_to_coding_delay_sec:
                        # Been idle long enough — load coding model
                        coding_card = self.apu_registry.get(cfg.coding_model)
                        if coding_card and not coding_card.is_on_gpu:
                            log.info(f"APU: idle for {cfg.idle_to_coding_delay_sec}s — transitioning to coding mode")
                            await self.apu_orchestrator.transition_to_coding_mode()

                elif self.state.voice == VoiceState.ATTENTIVE:
                    # Recently active — keep voice model, reset idle timer
                    idle_since = None

            except Exception as e:
                log.error(f"APU idle monitor error: {e}", exc_info=True)

    async def stop(self) -> None:
        """Shutdown gracefully."""
        log.info("Ada shutting down...")
        if goose_bridge.is_ready:
            await goose_bridge.stop()
        if self.config.apu.enabled:
            await self.apu_monitor.close()
        await self.outbox.stop()
        await self.store.end_session(self._session_id)
        await self.store.close()
        log.info("Ada stopped.")

    async def process_input(self, text: str, source_type: str = "user_voice") -> str:
        """Process one input through the Decision Engine. Returns spoken response.

        Full error handling: if any step fails, Ada still responds gracefully
        and logs the failure with context for debugging.
        """
        self._sequence += 1
        start_time = datetime.now()
        event_id = f"evt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self._sequence:05d}"

        # Update Sentinel probe context for richer error reports
        self.sentinel_probe.update_context(event_id=event_id, user_input=text)

        # Step 1: Classify intent
        try:
            cls_context = await self.context_builder.build_for_classification(
                session_id=self._session_id,
                user_message=text,
            )
        except Exception as e:
            log.error(f"[{event_id}] Context build failed: {e}", exc_info=True)
            self.sentinel_probe.capture_exception(e, "memory")
            cls_context = {"active_topic": "none", "pending_tasks": "none", "last_intent": "none"}

        try:
            classification = await classify_with_fallback(
                gateway=self.apu_gateway,
                model=self.config.models.control_model,
                user_message=text,
                **cls_context,
            )
        except Exception as e:
            log.error(f"[{event_id}] Classification failed completely: {e}", exc_info=True)
            self.sentinel_probe.capture_exception(e, "decision")
            classification = Classification(intent="noop", confidence=0.0, topic=text)
            await self.journal.log(
                action_type="error", summary=f"Classification pipeline failed: {e}",
                band=1, details={"event_id": event_id, "input": text[:200], "error": str(e)},
            )
            return "I had trouble understanding that. Could you say it again?"

        # Step 2: Resolve action (deterministic — should never fail, but guard anyway)
        try:
            resolver = ActionResolver(
                budget_checker=self._budget_checker(),
                state_checker=self._state_checker(),
            )
            plan = resolver.resolve(classification)
        except Exception as e:
            log.error(f"[{event_id}] Action resolution failed: {e}", exc_info=True)
            self.sentinel_probe.capture_exception(e, "decision")
            await self.journal.log(
                action_type="error", summary=f"Action resolver crashed: {e}",
                band=1, details={"event_id": event_id, "intent": classification.intent, "error": str(e)},
            )
            plan = ActionPlan(actions=[Action(action="respond_only", band=1)], band=1)

        # Step 3: Build Decision Event
        event = DecisionEvent(
            event_id=event_id,
            timestamp=start_time,
            sequence_num=self._sequence,
            source=Source(type=source_type, raw_input=text, turn_id=f"turn_{self._sequence:05d}"),
            classification=classification,
            decision=plan.actions[0] if plan.actions else Action(action="noop", band=1),
        )

        # Step 4: Persist decision event BEFORE execution (outbox FK depends on it)
        try:
            event.meta = Meta(
                processing_time_ms=0,
                model_used=self.config.models.control_model,
            )
            await self.store.insert_decision_event(event.to_dict())
        except Exception as e:
            log.error(f"[{event_id}] Failed to persist decision event: {e}", exc_info=True)
            self.sentinel_probe.capture_exception(e, "store")

        # Step 5: Execute action chain
        try:
            response = await self._execute_plan(event, plan)
        except Exception as e:
            log.error(f"[{event_id}] Action execution failed: {e}", exc_info=True)
            self.sentinel_probe.capture_exception(e, "executive")
            await self.journal.log(
                action_type="error", summary=f"Execution failed: {e}",
                band=2, details={"event_id": event_id, "action": event.decision.action, "error": str(e)},
            )
            response = "Something went wrong while handling that. I've logged the issue."

        # Step 6: Update decision event with final timing + log to journal
        try:
            elapsed = (datetime.now() - start_time).total_seconds() * 1000
            await self.journal.log(
                action_type="decision",
                summary=f"Intent: {classification.intent}, Action: {plan.actions[0].action if plan.actions else 'noop'}",
                band=plan.band,
                details={"intent": classification.intent, "confidence": classification.confidence,
                          "processing_ms": int(elapsed)},
            )
        except Exception as e:
            log.error(f"[{event_id}] Failed to log decision: {e}", exc_info=True)

        # Step 7: Store episodes with embeddings (non-blocking)
        try:
            user_embedding = await embed(text)
            await self.store.insert_episode(Episode(
                session_id=self._session_id,
                turn_type="user",
                speaker="Daniel",
                content=text,
                embedding=user_embedding,
                decision_event_id=event.event_id,
            ))
            if response:
                resp_embedding = await embed(response)
                await self.store.insert_episode(Episode(
                    session_id=self._session_id,
                    turn_type="ada",
                    speaker="Ada",
                    content=response,
                    embedding=resp_embedding,
                ))
        except Exception as e:
            log.error(f"[{event_id}] Failed to store episodes: {e}", exc_info=True)
            self.sentinel_probe.capture_exception(e, "memory")

        return response or ""

    async def process_input_streaming(self, text: str, push_sentence_fn, source_type: str = "user_voice") -> str:
        """Like process_input, but streams the response sentence-by-sentence.

        push_sentence_fn: async callable that pushes a sentence to TTS immediately.
        Returns the full response for episode storage.

        Fast path: classify + resolve + action (same as process_input)
        Streaming path: response generation streams tokens → sentences → TTS
        """
        self._sequence += 1
        start_time = datetime.now()
        event_id = f"evt_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{self._sequence:05d}"

        # Classification — use keyword fallback for speed in voice mode
        # LLM classification takes 5-15s which is too slow for voice.
        # Keyword fallback is instant and correct 80% of the time.
        # The response LLM handles nuance anyway.
        from .decision.classifier import _keyword_fallback
        classification = _keyword_fallback(text)

        # Resolve action (deterministic, ~1ms)
        resolver = ActionResolver(
            budget_checker=self._budget_checker(), state_checker=self._state_checker(),
        )
        plan = resolver.resolve(classification)

        # Build decision event
        event = DecisionEvent(
            event_id=event_id, timestamp=start_time, sequence_num=self._sequence,
            source=Source(type=source_type, raw_input=text, turn_id=f"turn_{self._sequence:05d}"),
            classification=classification,
            decision=plan.actions[0] if plan.actions else Action(action="noop", band=1),
        )

        # Persist decision event (FK for outbox)
        try:
            event.meta = Meta(processing_time_ms=0, model_used=self.config.models.control_model)
            await self.store.insert_decision_event(event.to_dict())
        except Exception:
            pass

        # Execute actions (notes, tasks, dispatch — non-response actions)
        try:
            await self._execute_plan(event, plan)
        except Exception as e:
            log.error(f"[{event_id}] Action execution failed: {e}")

        # Stream the response (this pushes sentences to TTS as they arrive)
        response = await self._generate_response_streaming(event, push_sentence_fn)

        # Store episodes
        try:
            from .memory.embeddings import embed
            user_embedding = await embed(text)
            await self.store.insert_episode(Episode(
                session_id=self._session_id, turn_type="user", speaker="Daniel",
                content=text, embedding=user_embedding, decision_event_id=event.event_id,
            ))
            if response:
                resp_embedding = await embed(response)
                await self.store.insert_episode(Episode(
                    session_id=self._session_id, turn_type="ada", speaker="Ada",
                    content=response, embedding=resp_embedding,
                ))
        except Exception:
            pass

        return response or ""

    async def _execute_plan(self, event: DecisionEvent, plan) -> str | None:
        """Execute an action plan. Returns spoken response if any.

        Chain scoping: note_id and task_id created in earlier actions are
        threaded through to later actions, so dispatch_agent dispatches
        the task THIS chain created, not some random active task.
        """
        response = None
        chain_note_id: str | None = None    # note created in this chain
        chain_task_id: str | None = None    # task created in this chain

        for action in plan.actions:
            if action.action == "respond_only":
                response = await self._generate_response(event)

            elif action.action == "ask_clarification":
                response = await self._generate_response(event, clarify=True)

            elif action.action == "create_note":
                from uuid import uuid4
                note = Note(
                    note_id=f"note_{uuid4().hex[:8]}",
                    content=event.source.raw_input,
                    type="actionable" if event.classification.note_action == "create" else "personal",
                    source="voice",
                )
                await self.store.insert_note(note)
                chain_note_id = note.note_id
                self._last_note_exists = True
                response = response or await self._generate_response(event)

            elif action.action == "create_task":
                # FORGE tasks need a repo_path for grounded coding
                target_agent = action.target or event.classification.target_agent
                repo_path = None
                if target_agent == "FORGE":
                    repo_path = self._infer_repo_path(event.classification.topic, event.source.raw_input)

                task = await self.task_manager.create_task(
                    title=event.classification.topic,
                    task_type=self._infer_task_type(event.classification),
                    origin="voice",
                    brief=event.source.raw_input,
                    dispatch_target=target_agent,
                    decision_event_id=event.event_id,
                    repo_path=repo_path,
                )
                chain_task_id = task.task_id

            elif action.action == "dispatch_agent":
                target_task_id = chain_task_id  # use task from THIS chain
                if not target_task_id:
                    # Fallback: find most recent task for this topic
                    tasks = await self.store.get_active_tasks()
                    target_task_id = tasks[0]["task_id"] if tasks else None
                if target_task_id:
                    await self.task_manager.dispatch_task(target_task_id, event.event_id)
                else:
                    log.warning(f"dispatch_agent: no task to dispatch for event {event.event_id}")
                response = response or await self._generate_response(event)

            elif action.action == "escalate_to_user":
                response = f"I need your input: {action.reason or 'ambiguous situation'}"

            elif action.action == "execute_command":
                response = await self._generate_response(event)

            elif action.action in ("noop", "save_silently"):
                pass

            elif action.action == "update_note":
                target_note_id = chain_note_id
                if not target_note_id:
                    notes = await self.store.get_active_notes()
                    target_note_id = notes[0]["note_id"] if notes else None
                if target_note_id:
                    # Fetch current content to append
                    notes = await self.store.get_active_notes()
                    current = next((n for n in notes if n["note_id"] == target_note_id), None)
                    if current:
                        new_content = f"{current.get('content', '')}\n- {event.source.raw_input}"
                        await self.store.update_note_status(target_note_id, current["status"], content=new_content)
                        await self.noteboard.broadcast_note_update(target_note_id, "edited", content=new_content)
                response = response or await self._generate_response(event)

            elif action.action == "promote_note":
                target_note_id = chain_note_id
                if not target_note_id:
                    notes = await self.store.get_active_notes()
                    target_note_id = notes[0]["note_id"] if notes else None
                if target_note_id:
                    notes = await self.store.get_active_notes()
                    note_data = next((n for n in notes if n["note_id"] == target_note_id), None)
                    if note_data:
                        task = await self.task_manager.promote_note_to_task(
                            target_note_id,
                            title=event.classification.topic or note_data.get("content", "")[:80],
                            brief=note_data.get("content", ""),
                        )
                        chain_task_id = task.task_id
                        await self.noteboard.broadcast_note_update(target_note_id, "promoted_to_task")
                        await self.noteboard.broadcast_task_update(task.task_id, "draft", task.title)

            elif action.action == "update_task":
                target_task_id = chain_task_id
                if not target_task_id:
                    tasks = await self.store.get_active_tasks()
                    target_task_id = tasks[0]["task_id"] if tasks else None
                if target_task_id:
                    task_data = await self.store.get_task(target_task_id)
                    if task_data:
                        await self.store.update_task_status(
                            target_task_id, "queued",
                            brief=f"{task_data.get('brief', '')}\nUSER DECISION: {event.source.raw_input}",
                        )
                        await self.noteboard.broadcast_task_update(target_task_id, "queued")
                response = response or await self._generate_response(event)

            elif action.action == "validate_result":
                tasks = await self.store.get_active_tasks()
                for t in tasks:
                    if t["status"] == "validating":
                        artifacts = json.loads(t.get("artifacts_received", "[]"))
                        result = await self.validator.validate(t, artifacts)
                        if result.passed:
                            await self.task_manager.complete_task(t["task_id"])
                            await self.noteboard.broadcast_task_update(t["task_id"], "done", t["title"])
                        else:
                            failed_gate = result.failed_gate
                            ok = await self.task_manager.retry_task(
                                t["task_id"], failed_gate.details if failed_gate else "validation failed",
                                event.event_id,
                            )
                            if not ok:
                                response = f"Task {t['task_id']} failed after max retries: {result.summary}"

            elif action.action == "retry_dispatch":
                tasks = await self.store.get_active_tasks()
                if tasks:
                    latest = tasks[0]
                    await self.task_manager.retry_task(
                        latest["task_id"], "Manual retry requested", event.event_id,
                    )
                response = response or await self._generate_response(event)

            elif action.action == "deliver_result":
                response = response or await self._generate_response(event)

            elif action.action == "queue_ultraplan":
                from .ultraplan.queue import PlanQueue
                queue = PlanQueue(self.store)
                await queue.submit(
                    title=event.classification.topic or event.source.raw_input[:80],
                    brief=event.source.raw_input,
                    domain=self._infer_domain(event.classification.topic),
                )
                await self.journal.log(
                    action_type="ultraplan_queued",
                    summary=f"Queued for overnight planning: {event.classification.topic}",
                    band=2,
                )
                response = response or "I'll plan this overnight with deep thinking. You'll have options in the morning."

        return response

    async def _generate_response(self, event: DecisionEvent, clarify: bool = False) -> str:
        """Generate spoken response via APU gateway.

        Uses multi-turn message format:
        1. System message: personality + compact context (tasks, memories)
        2. Conversation history as alternating user/assistant turns (from episodes)
        3. Current user message as the final user turn

        This gives the LLM real conversational memory — it sees prior turns
        as actual messages, not as text buried in the system prompt.
        """
        from .system_prompt import build_response_prompt
        from .memory.context_builder import ContextBuilder

        # Build system prompt with personality + context (current session only)
        active_tasks = await self.store.get_active_tasks()
        # Filter to tasks created this session (avoid stale hallucinations)
        session_start = datetime.strptime(self._session_id.replace("session_", ""), "%Y%m%d_%H%M%S").replace(tzinfo=None)
        active_tasks = [t for t in active_tasks
                        if t.get("created_at") and t["created_at"] >= session_start]
        pending = await self.store.fetch_pending_outbox(batch=5)

        # Skip memory search for simple turns (greetings, farewells) — saves ~500ms
        relevant_memories = []
        if event.classification.intent not in ("greeting", "farewell", "acknowledgement", "noop"):
            try:
                from .memory.embeddings import embed
                embedding = await embed(event.source.raw_input)
                mem_rows = await self.store.search_memories(embedding, limit=3)
                relevant_memories = [dict(r) for r in mem_rows]
            except Exception:
                pass

        system_prompt = build_response_prompt(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
            active_tasks=[dict(t) for t in active_tasks[:5]] if active_tasks else None,
            pending_deliveries=[dict(p) for p in pending] if pending else None,
            relevant_memories=relevant_memories if relevant_memories else None,
            instruction_files=self._load_instruction_files(),
        )

        # Build multi-turn message history from episodes
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]

        # Add recent conversation turns as real messages (current session only, last 10)
        episodes = await self.store.get_recent_episodes(self._session_id, limit=10)
        for ep in episodes:
            role = "user" if ep["speaker"] == "Daniel" else "assistant"
            messages.append({"role": role, "content": ep["content"]})

        # Add action context as a brief system hint (not a full instruction)
        if clarify:
            messages.append({"role": "user", "content": event.source.raw_input})
            messages.append({"role": "system", "content": "You're not sure what Daniel means. Ask a brief clarifying question."})
        else:
            action_hint = ""
            if event.decision.action == "create_note":
                action_hint = " [You just saved a note for him.]"
            elif event.decision.action == "dispatch_agent":
                action_hint = f" [You just dispatched this to {event.classification.target_agent or 'Arch'}.]"
            elif event.decision.action == "create_task":
                action_hint = " [You just created a task for this.]"

            messages.append({"role": "user", "content": event.source.raw_input + action_hint})

        try:
            return await self.apu_gateway.chat_response(
                model=self.config.models.control_model,
                messages=messages,
                temperature=0.7,
                timeout=30.0,
            )
        except Exception as e:
            log.error(f"Response generation failed: {e}")
            return "Got it."

    async def _generate_response_streaming(self, event: DecisionEvent, push_sentence_fn) -> str:
        """Stream response sentence-by-sentence for low-latency voice.

        Instead of waiting for the full response, this:
        1. Streams tokens from Ollama
        2. Buffers until a sentence boundary (. ! ? or newline)
        3. Calls push_sentence_fn(sentence) immediately — TTS starts generating
        4. Returns the full response for episode storage

        push_sentence_fn is an async callable that pushes a TextFrame to the pipeline.
        """
        from .system_prompt import build_response_prompt

        # Build system prompt (same as _generate_response)
        active_tasks = await self.store.get_active_tasks()
        # Filter to recent tasks (last 2 hours) to avoid stale hallucinations
        cutoff = datetime.now().astimezone() - __import__('datetime').timedelta(hours=2)
        active_tasks = [t for t in active_tasks
                        if t.get("created_at") and t["created_at"] >= cutoff]

        system_prompt = build_response_prompt(
            current_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
            active_tasks=[dict(t) for t in active_tasks[:5]] if active_tasks else None,
            instruction_files=self._load_instruction_files(),
        )

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        episodes = await self.store.get_recent_episodes(self._session_id, limit=10)
        for ep in episodes:
            role = "user" if ep["speaker"] == "Daniel" else "assistant"
            messages.append({"role": role, "content": ep["content"]})

        action_hint = ""
        if event.decision.action == "create_note":
            action_hint = " [You just saved a note.]"
        elif event.decision.action == "dispatch_agent":
            action_hint = f" [You just dispatched to {event.classification.target_agent or 'Arch'}.]"
        messages.append({"role": "user", "content": event.source.raw_input + action_hint})

        # Stream tokens, buffer into sentences, push each sentence to TTS
        full_response = ""
        sentence_buffer = ""
        sentence_endings = {'.', '!', '?'}

        try:
            async for token in self.apu_gateway.chat_stream(
                model=self.config.models.control_model,
                messages=messages,
                temperature=0.7,
                timeout=30.0,
            ):
                full_response += token
                sentence_buffer += token

                # Check for sentence boundary
                stripped = sentence_buffer.strip()
                if stripped and stripped[-1] in sentence_endings and len(stripped) > 5:
                    await push_sentence_fn(stripped)
                    log.info(f"[STREAM] Sentence pushed: {stripped[:60]}")
                    sentence_buffer = ""

            # Push any remaining text
            if sentence_buffer.strip():
                await push_sentence_fn(sentence_buffer.strip())
                log.info(f"[STREAM] Final chunk: {sentence_buffer.strip()[:60]}")

        except Exception as e:
            log.error(f"Streaming response failed: {e}")
            if not full_response:
                full_response = "Got it."
                await push_sentence_fn(full_response)

        return full_response

    def _load_instruction_files(self) -> str:
        """Load .ada/instructions.md files."""
        from pathlib import Path
        parts = []
        paths = [Path.home() / ".ada" / "instructions.md", Path(".ada") / "instructions.md"]
        for path in paths:
            if path.exists():
                content = path.read_text().strip()
                if len(content) > 6000:
                    content = content[:6000] + "\n[truncated]"
                parts.append(content)
        return "\n\n".join(parts) if parts else ""

    # --- Project path mapping for FORGE ---

    # Known project keywords → repo paths (on Daniel's machine)
    _PROJECT_PATHS = {
        "adaos": "/home/daniel/AdaOS",
        "ada": "/home/daniel/AdaOS",
        "sportwave": "/home/daniel/SportWave",
        "blackdirt": "/home/daniel/BlackDirt",
        "alchemy": "/home/daniel/NeoSynapticsOS/alchemy",
        "neodesktop": "/home/daniel/NeoDesktop-FlowView",
        "startrek": "/home/daniel/StarTrek",
        "autorender": "/home/daniel/AutoRender",
        "kemgas": "/home/daniel/Kemgas",
        "goldfish": "/home/daniel/GoldfishMemory",
        "rtxpointer": "/home/daniel/RTXPointer",
    }

    def _infer_repo_path(self, topic: str, raw_input: str) -> str | None:
        """Infer the target repo path from the topic/input text.

        Checks known project keywords first, then falls back to
        explicit path mentions in user input.
        """
        import os
        combined = f"{topic or ''} {raw_input or ''}".lower()

        # Check known projects
        for keyword, path in self._PROJECT_PATHS.items():
            if keyword in combined:
                if os.path.isdir(path):
                    return path

        # Check for explicit path in input (e.g. "work on /home/daniel/MyProject")
        import re
        path_match = re.search(r'(/[\w/.-]+)', raw_input or '')
        if path_match:
            candidate = path_match.group(1)
            if os.path.isdir(candidate):
                return candidate

        # Default: AdaOS itself (self-improvement is common)
        default = self._PROJECT_PATHS.get("adaos")
        if default and os.path.isdir(default):
            return default

        return None

    def _infer_domain(self, topic: str) -> str:
        """Infer UltraPlan domain from topic text."""
        lower = (topic or "").lower()
        if any(w in lower for w in ("sportwave", "sensor", "mmwave", "radar")):
            return "sportwave"
        if any(w in lower for w in ("blackdirt", "swarm", "dots", "rts")):
            return "blackdirt"
        if any(w in lower for w in ("adaos", "ada", "voice", "pipeline")):
            return "adaos"
        if any(w in lower for w in ("legal", "finca", "baratz")):
            return "legal"
        return "general"

    def _infer_task_type(self, c: Classification) -> str:
        if c.target_agent == "ARCH":
            return "architecture"
        if c.target_agent == "FORGE":
            return "implementation"
        return "research"

    def _budget_checker(self):
        """Real budget checker using cached daily token total from journal."""
        ada_ref = self

        class _BC:
            def allows_dispatch(self, target: str) -> bool:
                # Use the cached token count from APU gateway metrics
                if ada_ref.apu_gateway:
                    total_tokens = ada_ref.apu_gateway._total_tokens
                    # Rough estimate: 1 token ≈ $0.0000003 for DeepSeek V3.2
                    estimated_cost = total_tokens * 0.0000003
                    daily_cap = ada_ref.config.budget.daily_soft_cap_usd
                    if estimated_cost > daily_cap:
                        log.warning(f"Budget check: estimated ${estimated_cost:.2f} > daily cap ${daily_cap}")
                        return False
                return True
        return _BC()

    def _state_checker(self):
        """Real state checker using actual store data."""
        ada_ref = self

        class _SC:
            def has_active_note(self) -> bool:
                # Can't do async in sync context — check if we have a recent note
                # This is called from the resolver which is sync. Use a cached flag.
                return ada_ref._last_note_exists

            def awaiting_user_decision(self) -> bool:
                from ada.state import OverlayState
                return ada_ref.state.overlay == OverlayState.AWAITING_USER_DECISION
        return _SC()

    # --- Sentinel callbacks ---

    async def _sentinel_on_capture(self, probe_result, subsystem: str) -> None:
        """Called by Sentinel probe when an error is captured. Ingests into registry."""
        if not self.sentinel_registry:
            return
        try:
            report = await self.sentinel_registry.ingest(probe_result, subsystem)
            if report and self.config.sentinel.auto_export_markdown:
                await self._sentinel_export_report(report)
        except Exception as e:
            log.error(f"Sentinel ingest failed (non-fatal): {e}")

    async def _sentinel_export_report(self, report) -> None:
        """Export a sentinel report as a markdown file for LLM consumption."""
        from pathlib import Path
        export_dir = Path(self.config.sentinel.export_dir)
        export_dir.mkdir(parents=True, exist_ok=True)
        md = self.sentinel_report_builder.to_markdown(report)
        path = export_dir / f"{report.report_id}.md"
        path.write_text(md)
        log.info(f"Sentinel report exported: {path}")

    async def run_diagnostic(self, question: str) -> str:
        """User-facing: run a diagnostic investigation.

        Called when the user says something like:
        "diagnose memory consolidation"
        "I don't think the outbox is actually delivering"
        "check if the APU is swapping models correctly"
        """
        if not self.sentinel_diagnostics:
            return "Sentinel diagnostics not enabled. Check sentinel config."
        try:
            report = await self.sentinel_diagnostics.investigate(question)
            formatted = self.sentinel_diagnostics.format_report(report)
            if self.config.sentinel.auto_export_markdown:
                from pathlib import Path
                export_dir = Path(self.config.sentinel.export_dir)
                export_dir.mkdir(parents=True, exist_ok=True)
                path = export_dir / f"{report.diagnostic_id}.md"
                path.write_text(formatted)
            return formatted
        except Exception as e:
            log.error(f"Diagnostic failed: {e}", exc_info=True)
            self.sentinel_probe.capture_exception(e, "sentinel")
            return f"Diagnostic investigation failed: {e}"

    # ── WorldMonitor Integration ───────────────────────────────

    async def _on_worldmonitor_alert(self, alert: WMAlert) -> None:
        """Called by WorldMonitor connector when an alert threshold is crossed."""
        message = self.wm_briefing.generate_alert_message(alert)
        log.info(f"[WorldMonitor] {alert.severity}: {alert.title}")

        # Store as episode for memory
        try:
            episode = Episode(
                session_id=self._session_id,
                role="system",
                content=f"[WorldMonitor Alert] {message}",
                embedding=await embed(message),
            )
            await self.store.insert_episode(episode)
        except Exception as e:
            log.error(f"Failed to store WM alert episode: {e}")

        # Push to pending deliveries for voice notification
        self._pending_deliveries.append({
            "type": "worldmonitor_alert",
            "message": message,
            "severity": alert.severity,
            "category": alert.category,
        })

    async def _worldmonitor_briefing_loop(self) -> None:
        """Check if it's time for a scheduled briefing (morning/evening)."""
        delivered_today: set[str] = set()
        while True:
            await asyncio.sleep(60)  # check every minute
            try:
                now = datetime.now()
                hour = now.hour
                date_key = now.strftime("%Y-%m-%d")

                morning_key = f"{date_key}_morning"
                evening_key = f"{date_key}_evening"

                if hour == self.config.worldmonitor.morning_briefing_hour and morning_key not in delivered_today:
                    delivered_today.add(morning_key)
                    await self._deliver_briefing()
                elif hour == self.config.worldmonitor.evening_briefing_hour and evening_key not in delivered_today:
                    delivered_today.add(evening_key)
                    await self._deliver_briefing()

                # Clean old keys
                if len(delivered_today) > 20:
                    delivered_today.clear()

            except Exception as e:
                log.error(f"Briefing loop error: {e}")

    async def _deliver_briefing(self) -> None:
        """Fetch world snapshot and generate a voice briefing."""
        log.info("[WorldMonitor] Generating scheduled briefing...")
        try:
            snapshot = await self.wm_connector.get_briefing_data()
            briefing_text = self.wm_briefing.generate_daily_briefing(snapshot)

            # Store as episode
            episode = Episode(
                session_id=self._session_id,
                role="assistant",
                content=f"[Daily Briefing] {briefing_text}",
                embedding=await embed(briefing_text[:500]),
            )
            await self.store.insert_episode(episode)

            # Queue for voice delivery
            self._pending_deliveries.append({
                "type": "worldmonitor_briefing",
                "message": briefing_text,
            })
            log.info("[WorldMonitor] Briefing queued for voice delivery")

        except Exception as e:
            log.error(f"Briefing generation failed: {e}")

    async def get_world_status(self) -> str:
        """User-facing: get a quick world intelligence summary on demand."""
        try:
            snapshot = await self.wm_connector.get_briefing_data()
            return self.wm_briefing.generate_daily_briefing(snapshot)
        except Exception as e:
            log.error(f"World status failed: {e}")
            return "WorldMonitor is not available right now."

    async def get_sentinel_status(self) -> str:
        """User-facing: overview of Sentinel state — errors, patches, diagnostics."""
        lines = ["=== SENTINEL STATUS ===", ""]
        if self.sentinel_registry:
            stats = await self.sentinel_registry.get_stats()
            lines.extend([
                "--- Error Registry ---",
                f"  Total reports: {stats.get('total_reports', 0)}",
                f"  Unresolved: {stats.get('unresolved', 0)}",
                f"  Resolved: {stats.get('resolved', 0)}",
                f"  Unique signatures: {stats.get('unique_signatures', 0)}",
                f"  Total occurrences: {stats.get('total_occurrences', 0)}",
                "",
            ])
            unresolved = await self.sentinel_registry.get_unresolved(limit=5)
            if unresolved:
                lines.append("--- Top Unresolved ---")
                for r in unresolved:
                    lines.append(
                        f"  [{r.get('severity', '?')}] {r.get('title', '?')} "
                        f"(x{r.get('occurrence_count', 1)})"
                    )
                lines.append("")
        if self.sentinel_gate:
            pending = await self.sentinel_gate.get_pending()
            if pending:
                lines.append("--- Pending Patches ---")
                for p in pending:
                    lines.append(f"  {p.get('patch_id', '?')}: {p.get('description', '?')}")
                lines.append("")
        return "\n".join(lines)

    # --- Outbox deliverers ---

    async def _deliver_agent_dispatch(self, payload: dict) -> None:
        """Outbox deliverer: dispatch task to worker. Sentinel-wrapped."""
        task_id = payload["task_id"]
        try:
            result = await self.agent_dispatcher.dispatch(payload)
        except Exception as e:
            self.sentinel_probe.capture_exception(e, "agents")
            await self.task_manager.retry_task(task_id, f"Dispatch crashed: {e}", None)
            return

        if result.get("status") == "completed":
            artifacts = result.get("artifacts", [])
            await self.task_manager.handle_result(task_id, artifacts)
            await self.noteboard.broadcast_task_update(task_id, "validating")

            # Auto-validate (Sentinel-wrapped)
            task_data = await self.store.get_task(task_id)
            if task_data:
                try:
                    validation = await self.validator.validate(task_data, artifacts)
                except Exception as e:
                    self.sentinel_probe.capture_exception(e, "executive")
                    await self.task_manager.retry_task(task_id, f"Validation crashed: {e}", None)
                    return

                if validation.passed:
                    await self.task_manager.complete_task(task_id)
                    await self.noteboard.broadcast_task_update(task_id, "done", task_data.get("title", ""))
                    delivery_outbox = OutboxEvent(
                        decision_event_id=None,
                        event_type="delivery.queue",
                        payload={"task_id": task_id, "class": validation.delivery_class,
                                 "summary": validation.summary},
                    )
                    await self.store.insert_outbox(delivery_outbox)
                else:
                    failed = validation.failed_gate
                    feedback = failed.details if failed else "Validation failed"
                    await self.task_manager.retry_task(task_id, feedback, None)
        else:
            error = result.get("error", "Unknown worker failure")
            await self.task_manager.retry_task(task_id, error, None)

    async def _deliver_noteboard_push(self, payload: dict) -> None:
        """Outbox deliverer: push update to noteboard WebSocket clients."""
        await self.noteboard.broadcast(payload)

    async def _deliver_result_to_user(self, payload: dict) -> None:
        """Outbox deliverer: deliver validated result to Daniel.

        immediate → push to noteboard + speak via TTS if voice active
        delayed → store in journal for next greeting
        silent → journal only
        """
        delivery_class = payload.get("class", "delayed")
        task_id = payload.get("task_id", "")
        summary = payload.get("summary", "")

        # Always log to journal
        await self.journal.log(
            action_type="delivery",
            summary=f"[{delivery_class}] {task_id}: {summary}",
            task_id=task_id,
            band=1,
            details={"delivery_class": delivery_class, "summary": summary},
        )

        # Always push to noteboard
        await self.noteboard.broadcast_task_update(task_id, "delivered", summary[:100])

        if delivery_class == "immediate":
            from .state import VoiceState
            if self.state.voice in (VoiceState.ATTENTIVE, VoiceState.IDLE):
                # User is available — speak the result
                log.info(f"IMMEDIATE delivery (speaking): {task_id}")
                # Queue for next voice interaction — Ada will mention it
                self._pending_deliveries.append({
                    "task_id": task_id, "summary": summary, "class": "immediate",
                })
            else:
                # User is talking — don't interrupt, queue as delayed
                log.info(f"IMMEDIATE delivery (user busy, queued): {task_id}")
                self._pending_deliveries.append({
                    "task_id": task_id, "summary": summary, "class": "delayed",
                })
        elif delivery_class == "delayed":
            log.info(f"DELAYED delivery stored: {task_id}")
            self._pending_deliveries.append({
                "task_id": task_id, "summary": summary, "class": "delayed",
            })
        else:
            log.debug(f"SILENT storage: {task_id}")


async def run_text() -> None:
    """Run Ada in text-only mode (keyboard input)."""
    ada = Ada()
    await ada.start()

    print("Ada is running (text mode). Type to chat (Ctrl+C to quit).")
    print("---")

    try:
        while True:
            text = await asyncio.get_event_loop().run_in_executor(None, input, "You: ")
            if not text.strip():
                continue
            response = await ada.process_input(text.strip())
            print(f"Ada: {response}")
    except (KeyboardInterrupt, EOFError):
        print("\n---")
    finally:
        await ada.stop()


async def run_ui() -> None:
    """Run Ada with the web UI server."""
    from ui.server import start_server_async, set_ada

    ada = Ada()
    await ada.start()

    set_ada(ada, ada.config)
    print("Ada UI server starting on http://localhost:8765 ...")
    print("Open in browser or run: python ui/app.py")
    print("---")

    try:
        await start_server_async(ada, ada.config)
    except (KeyboardInterrupt, EOFError):
        print("\n---")
    finally:
        await ada.stop()


def main():
    mode = "text"
    if "--ui" in sys.argv:
        mode = "ui"

    if mode == "ui":
        asyncio.run(run_ui())
    else:
        asyncio.run(run_text())


if __name__ == "__main__":
    main()
