#!/usr/bin/env sh
set -eu

python -m evaluator.cli validate --tasks evaluation/tasks/tasks.json
python -m pytest -q tests/unit/test_evaluator.py tests/integration/test_evaluation_runner.py
