"""
A single deterministic "tool" the demo agent can call.

Kept intentionally simple and fake (no real API/database) so the project
runs with zero external dependencies beyond the Anthropic API — the point
of the demo is the observability layer, not the tool itself.

Themed as a customer-support lookup, since "agent answers a question,
sometimes needs to check a system, then answers" is a common real-world
shape for agents that need observability in production.
"""

from __future__ import annotations

# Anthropic tool-use schema for this tool.
# https://docs.anthropic.com/en/docs/build-with-claude/tool-use
GET_ORDER_STATUS_SCHEMA = {
    "name": "get_order_status",
    "description": (
        "Look up the current status of a customer order by its order ID. "
        "Use this whenever the user asks about the state, shipping, or "
        "delivery of a specific order."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "The order ID, e.g. 'ORD-1042'.",
            }
        },
        "required": ["order_id"],
    },
}

# Fake order database for the demo.
_FAKE_ORDERS = {
    "ORD-1042": {"status": "shipped", "carrier": "DHL", "eta_days": 2},
    "ORD-2077": {"status": "processing", "carrier": None, "eta_days": None},
    "ORD-3099": {"status": "delivered", "carrier": "UPS", "eta_days": 0},
}


def get_order_status(order_id: str) -> dict:
    """Execute the tool. Deterministic and side-effect free on purpose,
    so traces stay reproducible while the observability layer is being
    built out.
    """
    order = _FAKE_ORDERS.get(order_id)
    if order is None:
        return {"order_id": order_id, "found": False}
    return {"order_id": order_id, "found": True, **order}


TOOLS = [GET_ORDER_STATUS_SCHEMA]
TOOL_IMPLEMENTATIONS = {"get_order_status": get_order_status}
