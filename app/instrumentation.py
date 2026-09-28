"""
Core instrumentation module: wires OpenTelemetry spans around the agent's
model calls and tool calls, following the OTel GenAI semantic conventions.

What this module does
------------------------------------
Provides three context managers that `app/agent.py` calls at the right
points:

    with agent_span("customer-support-agent") as span:
        ...
        with chat_span(model="claude-...") as span:
            response = client.messages.create(...)
            record_chat_result(span, response, messages)
        ...
        with tool_span(tool_name="get_order_status", tool_input={...}) as span:
            result = get_order_status(...)
            record_tool_result(span, result)

Each opens a real OpenTelemetry span (via a tracer from
`opentelemetry.trace.get_tracer(...)`) and sets attributes per the OTel
GenAI semantic conventions. Span hierarchy per the spec:

    invoke_agent          (top-level span for one full agent run)
      chat                (one per model call)
      execute_tool        (one per tool call)

Attributes set (matching these names is what makes the traces show up
correctly in any tool built around this convention):

  On `chat` spans:
    gen_ai.request.model            e.g. "claude-sonnet-4-5"
    llm.call.purpose                # Pas standard - Ajout custom: "agent" or "evaluation"
    gen_ai.usage.input_tokens       from response.usage.input_tokens
    gen_ai.usage.output_tokens      from response.usage.output_tokens
    gen_ai.usage.total_tokens       # Pas standard - Ajout custom
    gen_ai.response.finish_reasons  e.g. [response.stop_reason]
    # Only if OTEL_CAPTURE_CONTENT=true (privacy/cost tradeoff — see .env.example):
    gen_ai.input.messages
    gen_ai.output.messages

  On `execute_tool` spans:
    gen_ai.tool.name
    gen_ai.tool.input                # Pas standard - Ajout custom
    gen_ai.tool.success               # Pas standard - Ajout custom
    # Only if OTEL_CAPTURE_CONTENT=true:
    gen_ai.tool.output               # Pas standard - Ajout custom

  On the top-level `invoke_agent` span:
    gen_ai.agent.name
    # Set separately by evaluation/evaluator.py once the run finishes:
    evaluation.flagged, evaluation.reasons

Two ways to wire the exporter
------------------------------
1. Manual (what this module does): a TracerProvider with an
   OTLPSpanExporter pointed at OTEL_EXPORTER_OTLP_ENDPOINT, registered as
   the global provider, with tracers pulled from it.
2. Auto-instrumentation (complementary, not a replacement): running the
   app via `opentelemetry-instrument` after `pip install
   elastic-opentelemetry && edot-bootstrap --action=install` provides
   HTTP-level spans and system metrics for free. It won't know about
   gen_ai.* semantics on its own — that's this module's job.
   https://www.elastic.co/observability-labs/blog/ml-ai-ops-observability-opentelemetry-elastic
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Optional

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (BatchSpanProcessor)
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
import json
import os

resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "llm-observability-demo")})
provider = TracerProvider(resource=resource)
processor = BatchSpanProcessor(OTLPSpanExporter())
provider.add_span_processor(processor)

# Sets the global default tracer provider
trace.set_tracer_provider(provider)

# Creates a tracer from the global tracer provider
tracer = trace.get_tracer("llm-observability-demo")

@contextmanager
def agent_span(agent_name: str) -> Iterator[Optional[Any]]:
    with tracer.start_as_current_span("invoke_agent") as span:
        span.set_attribute("gen_ai.agent.name", agent_name)
        yield span



@contextmanager
def chat_span(model: str, purpose: str = "agent") -> Iterator[Optional[Any]]:
    """`purpose` separates the agent's own model calls from the evaluator's
    LLM-as-judge call. Both are `chat` spans under the same `invoke_agent`,
    and both can use the same model, so gen_ai.request.model alone can't
    tell them apart — without this, the judge's tokens, latency and forced
    `tool_use` finish reason get blended into the agent's numbers.
    """
    with tracer.start_as_current_span("chat") as span:
        span.set_attribute("gen_ai.request.model", model)
        # Pas standard - Ajout custom
        span.set_attribute("llm.call.purpose", purpose)
        yield span


@contextmanager
def tool_span(tool_name: str, tool_input: dict) -> Iterator[Optional[Any]]:
    with tracer.start_as_current_span("execute_tool") as span:
        span.set_attribute("gen_ai.tool.name", tool_name)
        span.set_attribute("gen_ai.tool.input", json.dumps(tool_input))
        yield span


def record_chat_result(span: Optional[Any], response: Any, messages: list) -> None:
    # Extract token counts from the response
    input_tokens = response.usage.input_tokens
    output_tokens = response.usage.output_tokens
    total_tokens = input_tokens + output_tokens

    # Record on the span for per-request investigation
    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)

    # Pas standard - Ajout custom
    span.set_attribute("gen_ai.usage.total_tokens", total_tokens)

    # Extract the completion stop reason
    # Anthropic returns a string like 'end_turn', 'max_tokens', or 'stop_sequence'
    stop_reason = response.stop_reason

    # Official OTel Spec uses an array of strings for finish_reasons
    span.set_attribute("gen_ai.response.finish_reasons", [stop_reason])

    if os.environ.get("OTEL_CAPTURE_CONTENT", "false").lower() == "true":
            span.set_attribute("gen_ai.input.messages", json.dumps(messages, default=str))
            span.set_attribute("gen_ai.output.messages", json.dumps(response.content, default=str))


def record_tool_result(span: Optional[Any], result: dict) -> None:
    span.set_attribute("gen_ai.tool.success", result.get("found"))
    if os.environ.get("OTEL_CAPTURE_CONTENT", "false").lower() == "true":
        # Pas standard - Ajout custom
        span.set_attribute("gen_ai.tool.output", json.dumps(result))
