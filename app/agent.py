"""
Agent business logic — fully functional on its own (runs and answers
questions with zero instrumentation), so instrumentation.py can be built
against a real, working agent rather than a toy.

Deliberately not using a framework (LangChain, etc.) so the model calls
and tool-call loop stay visible and easy to wrap with spans, rather than
hidden inside a library's internals.
"""

from __future__ import annotations

import os

from anthropic import Anthropic

from app.instrumentation import agent_span, chat_span, tool_span, record_chat_result, record_tool_result
from app.tools import TOOLS, TOOL_IMPLEMENTATIONS
from evaluation.evaluator import evaluate

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

SYSTEM_PROMPT = (
    "You are a customer support assistant. Use the get_order_status tool "
    "whenever the user asks about a specific order. If they ask about an "
    "order without giving its ID, ask them for it. You have no information "
    "about store policies, returns, refunds or products: for any question "
    "that isn't about an order's status, say you can't answer it and "
    "suggest contacting a human agent — never answer it from general "
    "knowledge. Keep answers short."
)


def _add_usage(totals: dict, response) -> None:
    totals["input_tokens"] += response.usage.input_tokens
    totals["output_tokens"] += response.usage.output_tokens
    totals["total_tokens"] += response.usage.input_tokens + response.usage.output_tokens


def _finalize(
    top_span, user_message: str, answer: str, run_tokens: dict, tool_called: bool
) -> str:
    """Run the evaluator on the finished answer and attach its verdict to
    the invoke_agent span, so a flagged turn is visible right next to the
    trace it's about.
    """
    evaluation = evaluate(user_message, answer, run_tokens, tool_called)
    top_span.set_attribute("evaluation.flagged", evaluation.flagged)
    top_span.set_attribute("evaluation.reasons", evaluation.reasons)
    top_span.set_attribute("evaluation.judge_called", evaluation.judge_called)
    return answer


def run_agent(user_message: str) -> str:
    """Run one turn of the agent: ask the model, execute a tool if the
    model asks for one, ask the model again with the tool result, return
    the final text answer.

    This is the function main.py calls, and the one whose three phases
    (`invoke_agent` / `chat` / `execute_tool`) map directly onto the three
    context managers in instrumentation.py.
    """
    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    with agent_span("customer-support-agent") as top_span:
        messages = [{"role": "user", "content": user_message}]
        run_tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        with chat_span(model=MODEL) as span:
            response = client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
            record_chat_result(span, response, messages)
        _add_usage(run_tokens, response)

        # If the model didn't ask for a tool, we're done.
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            text_blocks = [b.text for b in response.content if b.type == "text"]
            answer = "\n".join(text_blocks)
            return _finalize(top_span, user_message, answer, run_tokens, tool_called=False)

        # Execute each requested tool call and feed results back.
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in tool_use_blocks:
            with tool_span(tool_name=block.name, tool_input=block.input) as span:
                impl = TOOL_IMPLEMENTATIONS[block.name]
                result = impl(**block.input)
                record_tool_result(span, result)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})

        with chat_span(model=MODEL) as span:
            final_response = client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
            record_chat_result(span, final_response, messages)
        _add_usage(run_tokens, final_response)

        text_blocks = [b.text for b in final_response.content if b.type == "text"]
        answer = "\n".join(text_blocks)
        return _finalize(top_span, user_message, answer, run_tokens, tool_called=True)
