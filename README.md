# Reliable Agent Runtime

A lightweight single-agent runtime built around explicit action schemas and controlled tools.

## Current milestone

Phase 1 provides a deterministic, read-only agent loop with:

- a validated tool registry;
- workspace-scoped `list_files`, `search_text`, and `read_file` tools;
- an in-memory multi-step loop backed by `FakeLLMClient`;
- structured step traces and a CLI demonstration.

## Run the phase 1 demo

Python 3.11 or newer is required.

```bash
python -m pip install -e '.[dev]'
reliable-agent \
  --workspace examples/sample_repo \
  --goal 'Find the order discount logic and explain the rules.' \
  --fake-actions examples/phase1_actions.json
```

## Verify

```bash
pytest
ruff check .
ruff format --check .
```
