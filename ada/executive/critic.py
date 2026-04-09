"""CRITIC_SERVICE — two-tier response scoring.

Tier 1: GPT-OSS self-scoring (baseline, always available)
  Generate 2-4 candidates, score them, pick best via policy code.

Tier 2: TRIBE v2 neural critic (experimental, the moat)
  Feed candidates through Meta FAIR brain encoder.
  Optimize for neural comprehension, not surface preference.
  NOT IMPLEMENTED YET — placeholder for research integration.
"""

import json
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass
class CandidateScore:
    candidate: str
    relevance: float        # 0-1: does it address the topic?
    clarity: float          # 0-1: is it clear and concise?
    tone: float             # 0-1: warm, natural, not robotic?
    urgency_match: float    # 0-1: does urgency level match context?
    overall: float = 0.0    # computed weighted score

    # Tier 2 (TRIBE) — populated when available
    neural_comprehension: float | None = None
    neural_confusion: float | None = None
    neural_engagement: float | None = None

    def compute_overall(self, tribe_weight: float = 0.0) -> None:
        base = (self.relevance * 0.35 + self.clarity * 0.30 +
                self.tone * 0.20 + self.urgency_match * 0.15)

        if tribe_weight > 0 and self.neural_comprehension is not None:
            neural = (
                self.neural_comprehension * 0.5 +
                (1.0 - (self.neural_confusion or 0.0)) * 0.3 +
                min(self.neural_engagement or 0.0, 0.8) * 0.2  # cap engagement — too much is bad
            )
            self.overall = base * (1 - tribe_weight) + neural * tribe_weight
        else:
            self.overall = base


SCORING_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "relevance": {"type": "number", "minimum": 0, "maximum": 1},
                    "clarity": {"type": "number", "minimum": 0, "maximum": 1},
                    "tone": {"type": "number", "minimum": 0, "maximum": 1},
                    "urgency_match": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["index", "relevance", "clarity", "tone", "urgency_match"]
            }
        }
    },
    "required": ["scores"]
}


class CriticService:
    def __init__(
        self,
        ollama_base_url: str = "",
        model: str = "",
        candidate_count: int = 4,
        tribe_enabled: bool = False,
        tribe_weight: float = 0.3,
        gateway=None,
    ):
        self.ollama_base_url = ollama_base_url
        self.model = model
        self.gateway = gateway
        self.candidate_count = candidate_count
        self.tribe_enabled = tribe_enabled
        self.tribe_weight = tribe_weight

    async def generate_and_score(
        self,
        context: str,
        user_message: str,
        intent: str,
        action: str,
    ) -> str:
        """Generate N candidates, score them, return the best one."""

        # Step 1: Generate candidates
        candidates = await self._generate_candidates(context, user_message, intent, action)
        if not candidates:
            return "Got it."
        if len(candidates) == 1:
            return candidates[0]

        # Step 2: Tier 1 — GPT-OSS self-scoring
        scores = await self._score_candidates_tier1(candidates, user_message, intent)

        # Step 3: Tier 2 — TRIBE (when available)
        if self.tribe_enabled:
            scores = await self._score_candidates_tier2(scores, candidates)

        # Step 4: Compute overall and pick best
        for s in scores:
            s.compute_overall(tribe_weight=self.tribe_weight if self.tribe_enabled else 0.0)

        best = max(scores, key=lambda s: s.overall)
        log.debug(f"Critic picked candidate with score {best.overall:.2f}")
        return best.candidate

    async def _generate_candidates(
        self, context: str, user_message: str, intent: str, action: str,
    ) -> list[str]:
        """Generate N candidate responses."""
        prompt = (
            f"Generate {self.candidate_count} different response options for Ada to say.\n"
            f"User said: \"{user_message}\"\n"
            f"Intent: {intent}, Action: {action}\n"
            f"Each response should be warm, concise, under 2 sentences.\n"
            f"Return a JSON array of strings. Vary tone and specificity."
        )

        candidates_schema = {
            "type": "object",
            "properties": {"responses": {"type": "array", "items": {"type": "string"}}},
            "required": ["responses"]
        }

        try:
            if self.gateway:
                result = await self.gateway.chat_json(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": context},
                        {"role": "user", "content": prompt},
                    ],
                    schema=candidates_schema,
                    temperature=0.8,
                    timeout=15.0,
                )
                return result.get("responses", [])[:self.candidate_count]
            else:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        f"{self.ollama_base_url}/api/chat",
                        json={
                            "model": self.model,
                            "messages": [
                                {"role": "system", "content": context},
                                {"role": "user", "content": prompt},
                            ],
                            "format": candidates_schema,
                            "stream": False,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    result = json.loads(data["message"]["content"])
                    return result.get("responses", [])[:self.candidate_count]
        except Exception as e:
            log.error(f"Candidate generation failed: {e}")
            return []

    async def _score_candidates_tier1(
        self, candidates: list[str], user_message: str, intent: str,
    ) -> list[CandidateScore]:
        """Tier 1: GPT-OSS self-scoring."""
        candidate_list = "\n".join(f"{i}: \"{c}\"" for i, c in enumerate(candidates))

        prompt = (
            f"Score each candidate response on 4 dimensions (0.0-1.0).\n"
            f"User said: \"{user_message}\" (intent: {intent})\n\n"
            f"Candidates:\n{candidate_list}\n\n"
            f"Score each on: relevance, clarity, tone, urgency_match."
        )

        try:
            if self.gateway:
                result = await self.gateway.chat_json(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    schema=SCORING_SCHEMA,
                    temperature=0.0,
                    timeout=15.0,
                )
            else:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        f"{self.ollama_base_url}/api/chat",
                        json={
                            "model": self.model,
                            "messages": [{"role": "user", "content": prompt}],
                            "format": SCORING_SCHEMA,
                            "stream": False,
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    result = json.loads(data["message"]["content"])

            scores = []
            for s in result.get("scores", []):
                idx = s["index"]
                if 0 <= idx < len(candidates):
                    scores.append(CandidateScore(
                        candidate=candidates[idx],
                        relevance=s["relevance"],
                        clarity=s["clarity"],
                        tone=s["tone"],
                        urgency_match=s["urgency_match"],
                    ))
            return scores

        except Exception as e:
            log.error(f"Tier 1 scoring failed: {e}")
            # Fallback: return all candidates with neutral scores
            return [
                CandidateScore(candidate=c, relevance=0.5, clarity=0.5, tone=0.5, urgency_match=0.5)
                for c in candidates
            ]

    async def _score_candidates_tier2(
        self, scores: list[CandidateScore], candidates: list[str],
    ) -> list[CandidateScore]:
        """Tier 2: TRIBE v2 neural critic.

        EXPERIMENTAL — NOT YET IMPLEMENTED.

        When implemented, this will:
        1. Feed each candidate (text or synthesized audio) through facebook/tribev2
        2. Get predicted fMRI activation (~70K voxels)
        3. Extract: comprehension, confusion, engagement signals
        4. Set neural_comprehension, neural_confusion, neural_engagement on each CandidateScore

        Research path:
        - Load tribev2 model from HuggingFace
        - Define "healthy neural activation" profile
        - Extract relevant brain region activations
        - A/B test against Tier 1-only selection
        """
        log.debug("TRIBE v2 Tier 2 scoring not yet implemented — using Tier 1 only")
        return scores
