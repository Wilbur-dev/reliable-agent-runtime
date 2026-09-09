# Reproducible evaluation

The task manifest contains 30 fixed tasks with the planned category distribution:

| Category | Count |
| --- | ---: |
| Code understanding | 5 |
| Configuration generation/repair | 5 |
| Single-file bug repair | 8 |
| Multi-file bug repair | 6 |
| Failure recovery | 3 |
| Security boundary | 3 |

Validate the dataset without a model:

```bash
python -m evaluator.cli validate --tasks evaluation/tasks/tasks.json
```

Run one smoke task against an OpenAI-compatible endpoint:

```bash
python -m evaluator.cli run \
  --tasks evaluation/tasks/tasks.json \
  --output evaluation/results \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen2.5-1.5B-Instruct \
  --profile reliable_runtime \
  --task-id understand-01
```

Run the complete four-profile matrix by omitting `--profile` and `--task-id`. This performs 120
task runs, so use the smoke task before committing GPU time.

Raw records are written below `evaluation/results/raw/<run_id>/`. Generate summaries without
modifying the raw records:

```bash
python -m evaluator.cli analyze \
  evaluation/results/raw/<run_id>/records.json \
  --output evaluation/results/summary
```

`Single Call`, `Basic Loop`, `Guarded Runtime`, and `Reliable Runtime` add tools, controls,
persistence, context management, and verification feedback incrementally. The report must identify
the exact model, endpoint settings, task manifest commit, and any incomplete runs.
