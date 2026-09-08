# Reliable Agent Runtime

A lightweight single-agent runtime built around explicit action schemas and controlled tools.

## Current milestone

Phase 2 adds a controlled code-modification and verification loop:

- a validated tool registry;
- workspace-scoped `list_files`, `search_text`, and `read_file` tools;
- protected-path-aware `apply_patch` and bounded `git_diff` tools;
- allowlisted `run_tests` and `run_linter` commands with timeouts;
- deterministic verifiers that decide completion independently of the model;
- an OpenAI-compatible client for cloud APIs and vLLM;
- structured model usage, latency, step, tool, and verification traces.

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

## Verify

```bash
pytest
ruff check .
ruff format --check .
```
