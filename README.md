# Reliable Agent Runtime

A lightweight single-agent runtime built around explicit action schemas and controlled tools.

## Current milestone

Phase 4 adds durable task execution and an HTTP API around the controlled loop:

- a validated tool registry;
- workspace-scoped `list_files`, `search_text`, and `read_file` tools;
- protected-path-aware `apply_patch` and bounded `git_diff` tools;
- allowlisted `run_tests` and `run_linter` commands with timeouts;
- deterministic verifiers that decide completion independently of the model;
- an OpenAI-compatible client for cloud APIs and vLLM;
- structured model usage, latency, step, tool, and verification traces.
- step, tool-call, wall-time, token, and estimated-cost budgets;
- bounded exponential backoff for retryable model errors;
- action fingerprinting, repeated-action detection, consecutive-failure circuit breaking, and
  no-progress termination;
- explicit termination reasons and structured retry evidence.
- SQLite/SQLAlchemy records for tasks, steps, messages, tool executions, artifacts, and
  verification results;
- transactional `PENDING -> RUNNING -> SUCCEEDED/FAILED` step checkpoints;
- startup recovery of interrupted work with workspace-hash-based mutation detection;
- FastAPI endpoints for task creation, execution, inspection, cancellation, and health checks.

## Run the phase 1 demo

Python 3.11 or newer is required.

```bash
python -m pip install -e '.[dev]'
reliable-agent \
  --mode readonly \
  --workspace examples/sample_repo \
  --goal 'Find the order discount logic and explain the rules.' \
  --fake-actions examples/phase1_actions.json
```

## Run the deterministic phase 2 demo

The Phase 2 fixture intentionally contains a bug. Copy it into a temporary Git repository so the
write loop can produce and verify a clean diff:

```bash
DEMO_WORKSPACE="$(mktemp -d)"
cp -R examples/order_service/. "$DEMO_WORKSPACE"
git -C "$DEMO_WORKSPACE" init -q
git -C "$DEMO_WORKSPACE" add .
git -C "$DEMO_WORKSPACE" \
  -c user.name=Demo \
  -c user.email=demo@example.com \
  commit -qm fixture

reliable-agent \
  --mode write \
  --workspace "$DEMO_WORKSPACE" \
  --goal 'Fix the gold discount so all tests pass. Do not modify tests/.' \
  --fake-actions examples/phase2_actions.json
```

The fixed action sequence deliberately makes an incorrect first patch. The model then requests
completion, verification rejects it, the failed test evidence returns to the loop, and the second
patch passes tests, lint, and protected-path verification.

## Connect an OpenAI-compatible model

Keep credentials in an environment variable rather than a command-line argument:

```bash
export OPENAI_API_KEY='...'

reliable-agent \
  --mode write \
  --workspace /path/to/git-workspace \
  --goal 'Fix the bug without modifying tests/.' \
  --base-url https://api.example.com/v1 \
  --model model-name
```

An empty API key is supported for local vLLM endpoints that do not require authentication.

## Configure reliability budgets

Budget limits are enforced by the Runtime, independently of model instructions:

```bash
reliable-agent \
  --mode write \
  --workspace /path/to/git-workspace \
  --goal 'Fix the bug without modifying tests/.' \
  --base-url https://api.example.com/v1 \
  --model model-name \
  --max-steps 20 \
  --max-tool-calls 40 \
  --max-wall-time 300 \
  --max-tokens 20000 \
  --max-cost-usd 0.50 \
  --max-llm-retries 2 \
  --input-cost-per-million 1.00 \
  --output-cost-per-million 2.00
```

Prices are explicit USD-per-million-token inputs. They are never fetched implicitly, so historical
experiments retain the estimation rule that produced their cost totals.

## Run the Phase 4 API

```bash
uvicorn app.api.main:app --host 127.0.0.1 --port 8080
```

Core endpoints:

```text
POST /tasks
POST /tasks/{id}/run
GET  /tasks/{id}
GET  /tasks/{id}/steps
POST /tasks/{id}/cancel
GET  /health
```

The API fixture accepts deterministic `fake_actions` so interruption and recovery behavior can be
reproduced in integration tests. Model-provider configuration can be injected above the same
Runtime service without changing the persistence schema.

## Verify

```bash
pytest
ruff check .
ruff format --check .
```
