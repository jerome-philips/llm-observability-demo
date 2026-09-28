"""
Evaluation layer: what differentiates "AI observability" from generic
APM. Traditional monitoring won't catch a runaway token cost or a
confidently wrong answer — this module is where that gets caught.

Three checks, from cheapest to most expensive:

1. Cost-anomaly detection (every turn): flag a turn whose total token
   usage exceeds a fixed threshold. Cheap, deterministic, no extra API
   call. A production version would compare against a rolling
   average/stddev per agent/tool combination rather than a fixed number
   — a threshold is enough to demonstrate the mechanism here.

2. No-tool-call rule (every turn): this agent's only source of facts is
   its order-lookup tool, so an answer produced without any tool call has
   nothing grounding it. Free and deterministic, but noisy on its own: a
   greeting, or asking the customer for their order ID, is also a
   legitimate answer without a tool call. So this rule doesn't flag by
   itself — it decides whether the judge runs.

3. LLM-as-judge quality check (only on turns the rule picked out): send
   the user's question and the agent's answer to a model with a rubric
   prompt, asking it to flag answers that look fabricated rather than
   grounded in what this agent can actually know. This is the check that
   catches a confidently wrong answer a cost or latency dashboard would
   never flag — and it also clears the rule's false positives.

   Running the judge on every turn used to cost roughly three quarters of the
   agent's own tokens on top of every answer, visible in Kibana once the
   judge's chat span was tagged `llm.call.purpose = "evaluation"`. Gating
   it behind the free rule keeps it off the turns that went through the
   tool. Trade-off: a hallucination added *after* a tool call (e.g. an
   invented delivery date on top of a real lookup) is no longer judged. A
   production setup would cover that with sampled judging of tool-backed
   turns.

The combined result is written back onto the current `invoke_agent` span
as `evaluation.flagged` / `evaluation.reasons`, plus
`evaluation.judge_called`, so a flagged turn is visible in Kibana right
next to the trace it's about, and the judge's call rate can be tracked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from anthropic import Anthropic

from app.instrumentation import chat_span, record_chat_result

# A separate, override-able model for the judge call — doesn't need to be
# the same model the agent itself uses.
EVAL_MODEL = os.environ.get("EVAL_MODEL", "claude-sonnet-4-5")

# Fixed threshold rather than a rolling average/stddev (see module
# docstring) — simple to reason about, enough for a demo.
COST_ANOMALY_THRESHOLD_TOKENS = 1200

JUDGE_SYSTEM_PROMPT = (
    "You are reviewing one answer from a customer support agent. The "
    "agent's only real capability is looking up order status through a "
    "tool, for a specific order ID — it has no return-policy documents, "
    "no general knowledge about this store, and no information beyond "
    "what a tool call could have given it. Flag the answer if it states "
    "specific policies, numbers, or procedures as fact without that "
    "grounding: that is a fabricated answer, not a correct one, even "
    "when it sounds plausible and confident."
)

SUBMIT_EVALUATION_SCHEMA = {
    "name": "submit_evaluation",
    "description": "Submit the evaluation verdict for the agent's answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "flagged": {
                "type": "boolean",
                "description": "True if the answer contains fabricated or ungrounded claims.",
            },
            "reasons": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short reasons supporting the verdict.",
            },
        },
        "required": ["flagged", "reasons"],
    },
}


@dataclass
class EvaluationResult:
    flagged: bool
    reasons: list[str] = field(default_factory=list)
    judge_called: bool = False


def _check_cost_anomaly(token_usage: dict) -> EvaluationResult:
    total = token_usage.get("total_tokens", 0)
    if total > COST_ANOMALY_THRESHOLD_TOKENS:
        return EvaluationResult(
            flagged=True,
            reasons=[
                f"total_tokens ({total}) exceeds the "
                f"{COST_ANOMALY_THRESHOLD_TOKENS}-token threshold"
            ],
        )
    return EvaluationResult(flagged=False)


def _check_llm_judge(user_message: str, agent_answer: str) -> EvaluationResult:
    client = Anthropic()
    judge_messages = [
        {
            "role": "user",
            "content": (
                f"User question:\n{user_message}\n\n"
                f"Agent answer (produced without any tool call):\n{agent_answer}"
            ),
        }
    ]

    # The judge call is itself a model call, so it gets its own chat span —
    # it shows up as a sibling of the agent's own chat spans rather than as
    # an untraced side effect, and its tokens count toward the run's cost.
    # purpose="evaluation" is what tells it apart from the agent's calls:
    # EVAL_MODEL and the agent's model can be the same, so
    # gen_ai.request.model isn't enough on its own.
    with chat_span(model=EVAL_MODEL, purpose="evaluation") as span:
        response = client.messages.create(
            model=EVAL_MODEL,
            max_tokens=256,
            system=JUDGE_SYSTEM_PROMPT,
            tools=[SUBMIT_EVALUATION_SCHEMA],
            tool_choice={"type": "tool", "name": "submit_evaluation"},
            messages=judge_messages,
        )
        record_chat_result(span, response, judge_messages)

    verdict_block = next(b for b in response.content if b.type == "tool_use")
    verdict = verdict_block.input
    return EvaluationResult(flagged=verdict["flagged"], reasons=verdict["reasons"])


def evaluate(
    user_message: str, agent_answer: str, token_usage: dict, tool_called: bool
) -> EvaluationResult:
    """Run the cost check, then the judge only if no tool was called."""
    cost_result = _check_cost_anomaly(token_usage)

    if tool_called:
        return EvaluationResult(
            flagged=cost_result.flagged,
            reasons=cost_result.reasons,
            judge_called=False,
        )

    judge_result = _check_llm_judge(user_message, agent_answer)
    judge_reasons = (
        ["answer produced without any tool call"] + judge_result.reasons
        if judge_result.flagged
        else []
    )
    return EvaluationResult(
        flagged=cost_result.flagged or judge_result.flagged,
        reasons=cost_result.reasons + judge_reasons,
        judge_called=True,
    )
