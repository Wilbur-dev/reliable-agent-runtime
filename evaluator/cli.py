from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

from evaluator.analysis import load_records, summarize, write_summary
from evaluator.dataset import load_tasks
from evaluator.models import EvaluationProfile, EvaluationRecord
from evaluator.runner import EvaluationRunner
from llm.openai_compatible import OpenAICompatibleClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run reproducible Agent Runtime evaluations.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="Validate the 30-task manifest.")
    validate.add_argument("--tasks", type=Path, default=Path("evaluation/tasks/tasks.json"))

    run = subparsers.add_parser("run", help="Run one or all evaluation profiles.")
    run.add_argument("--tasks", type=Path, default=Path("evaluation/tasks/tasks.json"))
    run.add_argument("--output", type=Path, default=Path("evaluation/results"))
    run.add_argument("--base-url", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--api-key-env", default="OPENAI_API_KEY")
    run.add_argument("--profile", choices=[*EvaluationProfile, "all"], default="all")
    run.add_argument("--task-id", action="append", default=[])

    analyze = subparsers.add_parser("analyze", help="Build JSON/CSV summaries from raw records.")
    analyze.add_argument("records", type=Path)
    analyze.add_argument("--output", type=Path, default=Path("evaluation/results/summary"))
    return parser


async def run_matrix(args: argparse.Namespace) -> int:
    tasks = load_tasks(args.tasks)
    if args.task_id:
        wanted = set(args.task_id)
        tasks = [task for task in tasks if task.id in wanted]
        missing = wanted - {task.id for task in tasks}
        if missing:
            raise SystemExit(f"Unknown task ids: {sorted(missing)}")
    profiles = list(EvaluationProfile) if args.profile == "all" else [args.profile]
    profiles = [EvaluationProfile(profile) for profile in profiles]
    run_id = uuid.uuid4().hex
    project_root = args.tasks.resolve().parents[2]
    runner = EvaluationRunner(project_root=project_root, output_dir=args.output)
    records: list[EvaluationRecord] = []
    for profile in profiles:
        for task in tasks:
            client = OpenAICompatibleClient(
                base_url=args.base_url,
                api_key=os.environ.get(args.api_key_env, ""),
                model=args.model,
                temperature=0.0,
                max_output_tokens=512,
            )
            records.append(
                await runner.run_task(
                    task,
                    profile,
                    client,
                    model=args.model,
                    run_id=run_id,
                )
            )
    combined = args.output / "raw" / run_id / "records.json"
    combined.parent.mkdir(parents=True, exist_ok=True)
    combined.write_text(
        json.dumps([record.model_dump(mode="json") for record in records], indent=2) + "\n",
        encoding="utf-8",
    )
    print(combined)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "validate":
        tasks = load_tasks(args.tasks)
        print(f"validated {len(tasks)} evaluation tasks")
        return 0
    if args.command == "run":
        return asyncio.run(run_matrix(args))
    records = load_records(args.records)
    args.output.mkdir(parents=True, exist_ok=True)
    summaries = summarize(records)
    write_summary(
        summaries,
        json_path=args.output / "summary.json",
        csv_path=args.output / "summary.csv",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
