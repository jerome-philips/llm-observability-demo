"""
Entry point. Runs a few example interactions through the agent to produce
a handful of varied traces (with and without tool calls, valid and
invalid order IDs) for inspection in Kibana.

Usage:
    python -m app.main
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from app.agent import run_agent  # noqa: E402  (must load .env first)
from opentelemetry import trace

EXAMPLE_QUERIES = [
    "What's the status of order ORD-1042?",
    "Can you check ORD-9999 for me?",
    "What's your return policy?",  # no tool call expected
    "Where's my order ORD-2077?",
    "Has ORD-3099 arrived yet?",
]


def main() -> None:
    for query in EXAMPLE_QUERIES:
        print(f"\n> {query}")
        answer = run_agent(query)
        print(answer)

    trace.get_tracer_provider().shutdown()    


if __name__ == "__main__":
    main()
