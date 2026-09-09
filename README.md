# LLM Observability Demo — Elastic + OpenTelemetry

Portfolio project: instrumenting a small tool-using LLM agent with the
OpenTelemetry GenAI semantic conventions, and observing it in Elastic
(Elasticsearch + Kibana) — reusing an Elastic/Kafka background instead of
adopting a brand-new observability stack (Langfuse/Arize) from scratch.

## Why this project exists

Traditional APM does not catch LLM-specific failure modes: runaway token
cost, silent hallucinations, or agents looping on a tool call. This demo
shows a minimal but realistic setup that does:

- an agent that calls a model, calls a tool, and answers — the three span
  levels defined by the OTel GenAI conventions (`invoke_agent`, `chat`,
  `execute_tool`)
- traces + cost/latency dashboards in Kibana
- a lightweight evaluation layer flagging anomalies an APM tool would miss

## Architecture

```
 app/main.py
   -> app/agent.py      (business logic: ask model, decide on tool use, answer)
        -> app/tools.py       (one deterministic demo tool)
        -> app/instrumentation.py   (OTel spans + gen_ai.* attributes)
        -> evaluation/evaluator.py  (flags cost/quality anomalies per turn)

        |  OTLP (gRPC, localhost:4317)
        v
 OpenTelemetry Collector (contrib)  [docker-compose.yml]
        |
        v
 Elasticsearch  <-----background----->  Kibana dashboards
```

See `docs/architecture.md` for the full data-flow diagram, including where
the evaluator's verdict lands on the trace.

## The two custom-built parts

Everything in this repo is written from scratch for the demo, but two
modules are the actual core of what it sets out to prove:

- `app/instrumentation.py` — the three span types (`invoke_agent`, `chat`,
  `execute_tool`) with attributes set per the OTel GenAI semantic
  conventions, plus a couple of clearly-marked non-standard additions
  (`gen_ai.usage.total_tokens`, `gen_ai.tool.output`). This is what makes
  the traces show up correctly in Kibana. See the module docstring for the
  full attribute list.
- `evaluation/evaluator.py` — a cost-anomaly check (fixed token threshold)
  and an LLM-as-judge quality check (a second model call, forced into
  structured output, reviewing the agent's answer for fabricated claims).
  The combined verdict is written back onto the `invoke_agent` span as
  `evaluation.flagged` / `evaluation.reasons`, so a flagged turn is visible
  right next to the trace that produced it.

## Setup

### 1. Local Elastic stack (Elasticsearch + Kibana + OTel Collector)

```bash
docker compose up -d
```

`docker-compose.yml` brings up Elasticsearch (`localhost:9200`), Kibana
(`localhost:5601`), and an OpenTelemetry Collector (`localhost:4317` gRPC,
`localhost:4318` HTTP) that forwards OTLP traces straight into
Elasticsearch using its native `elasticsearch` exporter, configured in
`otel-collector-config.yaml`. Security is disabled on Elasticsearch
(`xpack.security.enabled=false`) — acceptable for a stack bound to
localhost only; not a pattern to carry into anything reachable over a
network.

This uses the community `otel/opentelemetry-collector-contrib` image
rather than Elastic's own bundled EDOT Collector: the EDOT collector image
is built on a RHEL/UBI10 base that requires the x86-64-v3 CPU
microarchitecture level (AVX2 and friends), which fails outright on older
CPUs or on some Docker Desktop VM configurations that don't expose those
instructions to the guest. The contrib image doesn't carry that
requirement and is otherwise functionally equivalent for this project —
both are just running the standard OpenTelemetry Collector.

Sources: [OpenTelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib), [Elasticsearch exporter reference](https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/elasticsearchexporter/README.md)

### 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Environment variables

Copy `.env.example` to `.env` and fill in:
- `ANTHROPIC_API_KEY` — your Anthropic API key (a few cents of usage for a demo)
- `OTEL_EXPORTER_OTLP_ENDPOINT` — `http://localhost:4317` (matches the collector port above)
- `OTEL_SERVICE_NAME` — e.g. `llm-observability-demo`
- `EVAL_MODEL` — model used by the evaluator's LLM-as-judge check (independent from the agent's own model)

### 4. Run

```bash
python -m app.main
```

This sends traces to Elasticsearch via the OTel Collector, and runs each
example query's answer through the evaluator before printing it.

### 5. EDOT auto-instrumentation (optional, complementary)

Elastic also provides auto-instrumentation that captures spans without
code changes, which can complement your manual `gen_ai.*` spans with
system/process metrics and general HTTP call tracing:

```bash
pip install elastic-opentelemetry
edot-bootstrap --action=install
opentelemetry-instrument --service_name=llm-observability-demo python -m app.main
```

Source: [Elastic Observability Labs — ML/AI Ops Observability with OpenTelemetry and Elastic](https://www.elastic.co/observability-labs/blog/ml-ai-ops-observability-opentelemetry-elastic)

## Building the Kibana dashboards

Dashboard JSON isn't included here on purpose — the exact field mappings
depend on how you name your spans/attributes in `instrumentation.py`, so a
pre-baked export would likely not match. Once traces are flowing
(`Kibana > Discover`, check the `llm-observability-traces` index — that's
where `otel-collector-config.yaml` routes them), build:

1. **Cost over time**: a line/bar visualization summing
   `gen_ai.usage.input_tokens` + `gen_ai.usage.output_tokens` (multiplied
   by your model's price per token) bucketed by time.
2. **Latency per call type**: span duration, split by span name
   (`chat` vs `execute_tool`).
3. **Finish reason breakdown**: a pie/bar on `gen_ai.response.finish_reasons`
   to spot truncations or tool-call loops.
4. **Trace explorer**: Kibana's APM/trace view, filtered to your service
   name, to walk `invoke_agent -> chat -> execute_tool` per request.
5. **Anomaly view**: a saved search filtered on `evaluation.flagged: true`
   (set by `evaluator.py` on the `invoke_agent` span), so a flagged trace
   surfaces right next to the run it belongs to.

Export these as saved objects once built (`Stack Management > Saved
Objects > Export`) and commit the export into `kibana/dashboards/`.

## OTel GenAI semantic conventions reference

Key attributes (see `app/instrumentation.py` docstrings for the full list
used in this project):

- `gen_ai.request.model`
- `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`
- `gen_ai.response.finish_reasons`
- `gen_ai.system_instructions`, `gen_ai.input.messages`, `gen_ai.output.messages` (only if you enable content capture — mind API costs/privacy of logging full prompts)

Span hierarchy: `invoke_agent` (top-level) > `chat` (one per model call) >
`execute_tool` (one per tool call).

Sources: [OpenTelemetry Blog — Inside the LLM Call: GenAI Observability with OpenTelemetry](https://opentelemetry.io/blog/2026/genai-observability/)

## Status / next steps

- [x] Implement `app/instrumentation.py`
- [x] Run against local Elastic stack, confirm traces appear in Kibana
- [x] Build the dashboards above, export as saved objects
- [x] Implement `evaluation/evaluator.py` (cost anomaly and LLM-as-judge)
- [x] Write up the case study (`docs/case-study.md`)
