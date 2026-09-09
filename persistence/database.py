from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from runtime.actions import AgentAction
from runtime.control import action_fingerprint
from runtime.models import StepStatus, Task, TaskStatus, ToolResult


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), index=True)
    termination_reason: Mapped[str | None] = mapped_column(String(64))
    runtime_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    steps: Mapped[list[StepRow]] = relationship(cascade="all, delete-orphan")


class StepRow(Base):
    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    action: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True)
    workspace_hash_before: Mapped[str | None] = mapped_column(String(64))
    workspace_hash_after: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ToolExecutionRow(Base):
    __tablename__ = "tool_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id"), unique=True)
    tool_name: Mapped[str] = mapped_column(String(128))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON)
    result_summary: Mapped[str | None] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    diff: Mapped[str | None] = mapped_column(Text)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))


class VerificationResultRow(Base):
    __tablename__ = "verification_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    step_sequence: Mapped[int] = mapped_column(Integer)
    verifier: Mapped[str] = mapped_column(String(128))
    success: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON)


def utc_now() -> datetime:
    return datetime.now(UTC)


def workspace_hash(workspace: str | Path) -> str:
    root = Path(workspace).resolve()
    digest = hashlib.sha256()
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class SQLiteStore:
    def __init__(self, url: str = "sqlite:///reliable_agent.db") -> None:
        self.engine = create_engine(url)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    def create_task(self, task: Task, *, runtime_config: dict[str, Any] | None = None) -> Task:
        with self.sessions.begin() as session:
            session.add(
                TaskRow(
                    id=str(task.id),
                    payload=task.model_dump(mode="json"),
                    status=task.status.value,
                    termination_reason=None,
                    runtime_config=runtime_config or {},
                    metrics={},
                    created_at=task.created_at,
                    updated_at=task.updated_at,
                )
            )
        return task

    def get_task(self, task_id: str) -> Task | None:
        with self.sessions() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                return None
            payload = dict(row.payload)
            payload["status"] = row.status
            payload["termination_reason"] = row.termination_reason
            return Task.model_validate(payload)

    def get_runtime_config(self, task_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise KeyError(task_id)
            return dict(row.runtime_config)

    def get_task_record(self, task_id: str) -> dict[str, Any] | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        with self.sessions() as session:
            row = session.get(TaskRow, task_id)
            return {"task": task.model_dump(mode="json"), "metrics": dict(row.metrics)}

    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        termination_reason: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        with self.sessions.begin() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise KeyError(task_id)
            row.status = status.value
            row.termination_reason = termination_reason
            row.updated_at = utc_now()
            if metrics is not None:
                row.metrics = metrics

    def is_cancelled(self, task_id: str) -> bool:
        with self.sessions() as session:
            row = session.get(TaskRow, task_id)
            return bool(row and row.status == TaskStatus.CANCELLED.value)

    def begin_step(
        self,
        task: Task,
        sequence: int,
        action: AgentAction,
        *,
        mutates: bool,
    ) -> int:
        before_hash = workspace_hash(task.workspace) if mutates else None
        key = f"{action_fingerprint(action)}:{before_hash}" if mutates else None
        with self.sessions.begin() as session:
            row = StepRow(
                task_id=str(task.id),
                sequence=sequence,
                status=StepStatus.PENDING.value,
                action=action.model_dump(mode="json"),
                result=None,
                idempotency_key=key,
                workspace_hash_before=before_hash,
                workspace_hash_after=None,
                started_at=utc_now(),
                finished_at=None,
            )
            session.add(row)
            session.flush()
            step_id = row.id
        with self.sessions.begin() as session:
            session.get(StepRow, step_id).status = StepStatus.RUNNING.value
        return step_id

    def finish_step(
        self,
        step_id: int,
        result: ToolResult | None,
        *,
        task: Task,
        mutates: bool,
    ) -> None:
        payload = result.model_dump(mode="json") if result else None
        with self.sessions.begin() as session:
            row = session.get(StepRow, step_id)
            if row is None:
                raise KeyError(step_id)
            row.result = payload
            row.status = (
                StepStatus.SUCCEEDED.value
                if result is None or result.success
                else StepStatus.FAILED.value
            )
            row.workspace_hash_after = workspace_hash(task.workspace) if mutates else None
            row.finished_at = utc_now()
            if result is not None:
                rendered = json.dumps(payload, sort_keys=True)
                session.add(
                    ToolExecutionRow(
                        step_id=step_id,
                        tool_name=row.action.get("tool_name", ""),
                        arguments=row.action.get("arguments", {}),
                        result_summary=(result.output or result.error_message or "")[:4000],
                        exit_code=result.exit_code,
                        content_hash=hashlib.sha256(rendered.encode()).hexdigest(),
                        diff=result.output if row.action.get("tool_name") == "git_diff" else None,
                    )
                )
                if mutates and result.success:
                    for changed_path in result.metadata.get("changed_paths", []):
                        absolute = Path(task.workspace) / changed_path
                        if absolute.is_file():
                            session.add(
                                ArtifactRow(
                                    task_id=str(task.id),
                                    kind="modified_file",
                                    path=changed_path,
                                    content_hash=hashlib.sha256(absolute.read_bytes()).hexdigest(),
                                )
                            )

    def replace_messages(self, task_id: str, messages: list[dict[str, Any]]) -> None:
        with self.sessions.begin() as session:
            session.query(MessageRow).filter_by(task_id=task_id).delete()
            session.add_all(
                MessageRow(task_id=task_id, position=index, payload=message)
                for index, message in enumerate(messages)
            )

    def save_verifications(
        self, task_id: str, step_sequence: int, results: list[dict[str, Any]]
    ) -> None:
        with self.sessions.begin() as session:
            session.add_all(
                VerificationResultRow(
                    task_id=task_id,
                    step_sequence=step_sequence,
                    verifier=result["verifier"],
                    success=int(result["success"]),
                    evidence=result["evidence"],
                    metadata_json=result.get("metadata", {}),
                )
                for result in results
            )

    def list_steps(self, task_id: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(StepRow).where(StepRow.task_id == task_id).order_by(StepRow.sequence)
            ).all()
            return [
                {
                    "id": row.id,
                    "sequence": row.sequence,
                    "status": row.status,
                    "action": row.action,
                    "result": row.result,
                    "idempotency_key": row.idempotency_key,
                    "workspace_hash_before": row.workspace_hash_before,
                    "workspace_hash_after": row.workspace_hash_after,
                }
                for row in rows
            ]

    def load_messages(self, task_id: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(MessageRow)
                .where(MessageRow.task_id == task_id)
                .order_by(MessageRow.position)
            ).all()
            return [dict(row.payload) for row in rows]

    def recover_interrupted(self) -> list[str]:
        recovered: list[str] = []
        with self.sessions.begin() as session:
            tasks = session.scalars(
                select(TaskRow).where(TaskRow.status == TaskStatus.RUNNING.value)
            ).all()
            for task in tasks:
                next_message_position = len(
                    session.scalars(select(MessageRow).where(MessageRow.task_id == task.id)).all()
                )
                running_steps = session.scalars(
                    select(StepRow).where(
                        StepRow.task_id == task.id,
                        StepRow.status == StepStatus.RUNNING.value,
                    )
                ).all()
                for step in running_steps:
                    current_hash = workspace_hash(task.payload["workspace"])
                    changed = bool(
                        step.workspace_hash_before and current_hash != step.workspace_hash_before
                    )
                    if changed:
                        step.status = StepStatus.SUCCEEDED.value
                        step.workspace_hash_after = current_hash
                        step.result = ToolResult(
                            success=True,
                            metadata={"recovered_from_workspace_hash": True},
                        ).model_dump(mode="json")
                        session.add_all(
                            [
                                MessageRow(
                                    task_id=task.id,
                                    position=next_message_position,
                                    payload={"role": "assistant", "content": step.action},
                                ),
                                MessageRow(
                                    task_id=task.id,
                                    position=next_message_position + 1,
                                    payload={
                                        "role": "tool",
                                        "name": step.action.get("tool_name", "recovered_tool"),
                                        "content": step.result,
                                    },
                                ),
                            ]
                        )
                        next_message_position += 2
                    else:
                        step.status = StepStatus.INTERRUPTED.value
                        step.idempotency_key = None
                    step.finished_at = utc_now()
                task.status = TaskStatus.PENDING.value
                task.updated_at = utc_now()
                recovered.append(task.id)
        return recovered
