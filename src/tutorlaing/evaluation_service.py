from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .ai import AIClient, AIError, ResponseAnalysis
from .content import Scenario
from .contracts import LanguageStore
from .difficulty import practice_level
from .engine import Evaluation, evaluate_response


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluationResult:
    evaluation: Evaluation
    analysis: ResponseAnalysis | None = None
    analysis_id: int | None = None


class ResponseEvaluator:
    """Evaluate a learner response and persist optional AI enrichment.

    Deterministic rules remain the fallback and the source of matched/missing
    semantic groups.  AI may adjust only the score and learner-facing feedback.
    """

    def __init__(self, store: LanguageStore, ai: AIClient | None):
        self.store = store
        self.ai = ai

    def evaluate(
        self,
        chat_id: int,
        user: Any,
        scenario: Scenario,
        step_index: int,
        response: str,
    ) -> EvaluationResult:
        step = scenario.steps[step_index]
        rule_evaluation = evaluate_response(step, response)
        if self.ai is None:
            return EvaluationResult(rule_evaluation)

        self.store.event(
            chat_id,
            "ai_analysis_requested",
            {"operation": "response_analysis", "scenario_id": scenario.id},
        )
        try:
            analysis = self.ai.analyze_response(
                step,
                response,
                str(user["instruction_language"]),
                str(user["target_language"]),
                rule_evaluation.score,
                practice_level(user),
            )
        except AIError:
            LOGGER.exception("AI response analysis failed")
            self.store.event(
                chat_id,
                "ai_fallback_used",
                {"operation": "response_analysis", "scenario_id": scenario.id},
            )
            return EvaluationResult(rule_evaluation)

        score = analysis.score
        if analysis.task_achieved and score < 0.6:
            score = 0.6
        elif not analysis.task_achieved and score >= 0.6:
            score = 0.59
        evaluation = Evaluation(
            score,
            rule_evaluation.matched_groups,
            rule_evaluation.missing_groups,
        )
        analysis_id = self.store.add_ai_analysis(
            chat_id=chat_id,
            session_id=str(user["current_session"]) if user["current_session"] else None,
            scenario_id=scenario.id,
            step_index=step_index,
            operation="response_analysis",
            target_language=str(user["target_language"]),
            source_text=response,
            result=analysis.to_dict(),
            provider=analysis.provider,
            model=analysis.model,
            prompt_version=analysis.prompt_version,
            latency_ms=analysis.latency_ms,
            usage=analysis.usage,
        )
        self.store.event(
            chat_id,
            "ai_analysis_completed",
            {
                "operation": "response_analysis",
                "scenario_id": scenario.id,
                "model": analysis.model,
                "latency_ms": analysis.latency_ms,
            },
        )
        return EvaluationResult(evaluation, analysis, analysis_id)
