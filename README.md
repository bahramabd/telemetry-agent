# Telemetry Intelligence Agent

A conversational AI agent that reasons over distributed system telemetry from an e-commerce microservices stack. Ask questions in natural language; The agent queries real MongoDB telemetry data, computes deterministic answers, and optionally synthesizes rich narrative responses using an LLM.

Built for the **Decision Compute Infrastructure** assessment.

---

## What It Does

The agent connects to a MongoDB database containing OpenTelemetry-compatible spans, logs, and metrics from a simulated e-commerce platform. It supports two primary capabilities:

**System Health Queries**
```
> What is the overall health of the system right now?
> Which service has the highest error rate in the last hour?
> Is the checkout flow operating within normal latency bounds?
> What is the p99 latency for the order service?
> What is the p95 latency for all services in the last 30 minutes?
```

**Root Cause Analysis**
```
> Run RCA on the most recent incident you can find in the data
> The checkout flow was degraded between 14:00 and 14:45 — what happened?
> Run RCA from 2pm to 2:45pm
> What was the root cause of the last incident?
```

---

## Quick Start — Docker (Recommended)

The agent runs as a Docker container. MongoDB must be running on your host machine with the telemetry dataset imported.

## Running The Agent

**Linux / macOS:**
```bash
docker run -it \
  --env MONGO_URI=mongodb://host.docker.internal:27017/telemetry \
  --env LLM_API_KEY=your-api-key \
  telemetry-agent
```

**Windows (PowerShell):**
```powershell
docker run -it `
  --env MONGO_URI=mongodb://host.docker.internal:27017/telemetry `
  --env LLM_API_KEY=your-api-key `
  telemetry-agent
```

> On Linux/macOS use `\` for line continuation. On Windows PowerShell use `` ` ``.
> On Windows Command Prompt (cmd.exe) use `^` instead.

### With LLM synthesis enabled (richer narrative answers)

```bash
docker run -it `
  --env MONGO_URI=mongodb://host.docker.internal:27017/telemetry `
  --env LLM_API_KEY=<your-api-key> `
  --env ENABLE_LLM_SYNTHESIS=true `
  telemetry-agent
```

> **Note:** `ENABLE_LLM_SYNTHESIS=false` (the default) still uses the LLM for intent parsing — it only disables the optional final-answer narrative rewrite. The agent works correctly and produces detailed evidence-based answers either way. Enabling synthesis adds richer prose explanations at the cost of slightly longer response times (~2–4 seconds extra per query). 


## Running Locally (Without Docker)

### Prerequisites

- Python 3.11+
- MongoDB running locally with the telemetry dataset imported
- An OpenAI or Anthropic API key

### 1. Import the dataset

```bash
mongoimport --db telemetry --collection spans   --file spans.json
mongoimport --db telemetry --collection logs    --file logs.json
mongoimport --db telemetry --collection metrics --file metrics.json
```

### 2. Set up the environment

```bash
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate  # macOS/Linux

pip install -r requirements.txt
```

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your API key:

```env
MONGO_URI=mongodb://localhost:27017/telemetry
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-proj-your-key-here
ENABLE_LLM_SYNTHESIS=false
DEBUG_INTENT=false
```

### 4. Run

```bash
python -m src.main
```

## The System Under Test

An e-commerce platform with 4 microservices:

- **catalog-service** — product listings with personalisation signals from cart
- **cart-service** — user cart state and item management  
- **order-service** — checkout orchestration (cart → payments → fulfilment)
- **payments-service** — payment processing, calls MongoDB for transactions

Two flows: **Listing** (catalog → cart) and **Checkout** (order → cart → payments → MongoDB).

---

## Architecture

### System Overview

```
User question (natural language)
         ↓
┌─────────────────────────────┐
│      Intent Parser          │  LLM parses question → structured intent JSON
│  (LLM primary / regex       │  Fallback: deterministic regex if LLM fails
│   fallback)                 │
└─────────────────────────────┘
         ↓
┌─────────────────────────────┐
│    Time Range Resolver      │  Converts time expressions to datetime objects
│  (time_utils.py)            │  Supports: "last 12 minutes", "14:00 to 14:45",
│                             │  "from 2pm to 2:45pm", relative + explicit ranges
└─────────────────────────────┘
         ↓
┌─────────────────────────────┐
│   Deterministic Telemetry   │  Queries MongoDB directly
│   Analysis Layer            │  health.py / rca.py / traces.py / logs.py / metrics.py
│                             │  Computes: error rates, p50/p95/p99 latency,
│                             │  incident detection, baseline comparison,
│                             │  cascade timeline, resource anomalies
└─────────────────────────────┘
         ↓
┌─────────────────────────────┐
│   Answer Formatter          │  Deterministic structured answer (always runs)
│   (agent.py)                │  Rich terminal output with service breakdown tables
└─────────────────────────────┘
         ↓ (if ENABLE_LLM_SYNTHESIS=true)
┌─────────────────────────────┐
│   LLM Answer Synthesizer    │  Rewrites deterministic answer into natural prose
│   (answer_synthesizer.py)   │  Uses slim evidence bundle — no duplicate queries
│                             │  Falls back to deterministic answer if LLM fails
└─────────────────────────────┘
         ↓
     Terminal output
```

### Key Design Decisions

#### 1. Deterministic truth layer — LLM never touches the database

All telemetry calculations (error rates, percentiles, incident detection, baseline comparisons) are computed by Python functions backed by MongoDB aggregation pipelines. The LLM never queries MongoDB directly and never calculates statistics. This eliminates hallucination of telemetry values.

#### 2. Two-layer LLM usage

The LLM is used for two separate jobs, each with independent fallbacks:

- **Intent parsing** — converts natural language into a structured `AgentIntent` object (intent type, service name, time range). Falls back to a deterministic regex parser if the LLM is unavailable or returns invalid JSON.
- **Answer synthesis** — optionally rewrites the deterministic answer into readable prose. Falls back to the structured deterministic answer if synthesis fails or is disabled.

This means the agent works fully offline (without any LLM calls) via the deterministic fallback path — it just uses the structured formatter instead of narrative prose.

#### 3. Structured intent with Pydantic validation

LLM intent responses are validated against a strict Pydantic schema. If the LLM returns an unrecognized service name, an invalid metric, or malformed JSON, the validation fails silently and the regex fallback takes over. This prevents bad LLM output from reaching the database layer.

#### 4. Static dataset — time handling

The telemetry dataset is historical (2026-05-26). "Right now" and "the last hour" are resolved relative to the **latest timestamp in the dataset** (18:00), not the real current time. This is noted in the agent's startup output and handled transparently in `time_utils.py`. The dataset end time is computed dynamically from MongoDB — not hardcoded.

#### 5. Partial statefulness — conversation history

The agent maintains a rolling window of the last 3 user questions and passes them to the LLM intent parser as context. This enables natural follow-up questions:

```
> Run RCA between 14:00 and 14:45
  ... (RCA output) ...
> What about 13:00 to 13:30?   ← agent understands this means "RCA for that window"
```

History contains questions only (not answers) to keep prompt size small.

#### 6. Dual LLM provider support

The agent supports both OpenAI and Anthropic via a unified `call_llm()` interface. Switch providers by setting `LLM_PROVIDER=anthropic` and providing `ANTHROPIC_API_KEY`. The agent was built and tested using **OpenAI gpt-4o-mini**.


### Supported Intents

| Intent | Example queries |
|---|---|
| `overall_health` | "Is the system healthy?", "What is the overall status?" |
| `highest_error_rate` | "Which service has the most errors?", "Highest error rate in the last hour?" |
| `service_latency` | "p99 for order-service?", "What is the latency across all services?" |
| `checkout_health` | "Is checkout working normally?", "Checkout flow latency bounds?" |
| `rca_recent_incident` | "Run RCA", "What happened between 14:00 and 14:45?", "Root cause of last incident" |

### RCA Evidence Sources

When running root cause analysis, the agent correlates all three telemetry signal types:

- **Spans** → where failures occurred, which operations are slow or erroring, cascade direction
- **Logs** → why failures occurred (connection pool timeouts, database errors, retry exhaustion)
- **Metrics** → whether CPU or memory pressure was a contributing factor

The RCA output includes: incident detection, severity, affected flow, probable root cause, confidence level, baseline comparison (incident vs normal latency multiplier), observed failure timeline, blast radius, and recommended next steps.

---

## Building The Docker Image

```bash
docker build -t telemetry-agent .
```
## Loading The Docker Image

```bash
docker load -i telemetry-agent.tar
```
