# Architecture notes

## Data flow

```
User query
   |
   v
app/main.py  --calls-->  app/agent.py::run_agent()
   |
   |  invoke_agent span (whole run)
   |
   +--> chat span --------> Anthropic API (messages.create)
   |        records: gen_ai.request.model, gen_ai.usage.*,
   |                 gen_ai.response.finish_reasons
   |
   +--> execute_tool span -> app/tools.py::get_order_status()
   |        records: gen_ai.tool.name, gen_ai.tool.input, gen_ai.tool.success
   |
   +--> chat span (2nd call, with tool result folded in)
   |
   +--> evaluation/evaluator.py::evaluate()  (cost threshold + LLM-as-judge)
   |        the judge call is itself a chat span; the combined verdict is
   |        written back onto the invoke_agent span as evaluation.flagged /
   |        evaluation.reasons before returning
   |
   v
Final answer returned to caller
   |
   v (async, via OTel SDK batching)
OTLP exporter -> OpenTelemetry Collector (contrib), localhost:4317 [docker-compose.yml]
   |
   v
Elasticsearch  <---- Kibana reads from here for dashboards/trace explorer
```

## Why three span levels

This mirrors the OTel GenAI semantic conventions' span hierarchy
(`invoke_agent` > `chat` > `execute_tool`), rather than one flat span per
request. The payoff shows up in Kibana's trace view: you can see, for a
single slow or expensive user request, whether the time/cost went into
the model thinking, a tool call, or a second round-trip to the model —
which is exactly the diagnostic traditional APM can't give you for an
LLM-based system.

## Why the evaluator writes onto the trace, not a separate report

A cost or latency dashboard has no way to catch a confidently wrong
answer — a hallucinated response isn't slower or more expensive to
produce than a correct one. `evaluation/evaluator.py` runs an LLM-as-judge
check (plus a cheap token-count threshold) on every turn and attaches the
verdict directly to that turn's `invoke_agent` span. That keeps the
flagged answer next to the full trace that produced it, instead of living
in a disconnected offline report.

## What to screenshot for the case study write-up

- The trace explorer view showing `invoke_agent -> chat -> execute_tool`
  for one request.
- The cost-over-time dashboard, ideally with an artificial spike you can
  point to and explain.
- A trace the evaluator flagged, with the flagged reason next to it.

A screenshot showing everything green is far less compelling than one
showing a problem this setup actually caught.
