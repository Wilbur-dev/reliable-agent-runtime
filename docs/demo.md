# Five-minute demonstration

## 1. Show deterministic regression safety

```bash
python -m pytest -q
python -m evaluator.cli validate --tasks evaluation/tasks/tasks.json
```

Explain that Fake LLM scenarios cover state transitions deterministically; real-model experiments
are stored separately and are not the only CI signal.

## 2. Run the repair loop

Copy `examples/order_service` to a temporary Git repository and run `examples/phase2_actions.json`
with the write-mode CLI. Point out the first incorrect patch, failed verification evidence, second
repair, protected tests, and final verified completion.

## 3. Show governance controls

Use the API tests to demonstrate a protected-path denial and a dependency change entering
`WAITING_APPROVAL`. Approval is bound to a step and action fingerprint; rejection is returned to
the Agent as structured feedback.

## 4. Show recovery and context evidence

Query `/tasks/{id}/steps`, `/events`, and `/context-compactions`. Contrast the complete SQLite
trajectory with the shortened model context and show a full tool log retrieved by `result_id`.

## 5. Close with measured claims

Open the raw evaluation records and generated CSV summary. Discuss verified success, false
completion, recovery, policy blocks, latency, tokens, and known limitations rather than relying on
a single successful demo.
