from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_prompt_chars: int = Field(default=16_000, ge=1_000)
    recent_messages: int = Field(default=8, ge=2)
    max_tool_output_chars: int = Field(default=2_000, ge=200)
    summary_max_chars: int = Field(default=4_000, ge=500)


class ContextBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]]
    compacted: bool = False
    chars_before: int = Field(ge=0)
    chars_after: int = Field(ge=0)
    estimated_tokens_before: int = Field(ge=0)
    estimated_tokens_after: int = Field(ge=0)
    compacted_message_count: int = Field(default=0, ge=0)
    result_ids: list[str] = Field(default_factory=list)
    stale_file_references: list[str] = Field(default_factory=list)


def serialized_chars(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, sort_keys=True))


def estimate_tokens(char_count: int) -> int:
    # Deterministic approximation suitable for comparing compaction before/after.
    return (char_count + 3) // 4


class ContextBuilder:
    """Build a bounded model view without deleting the durable full trajectory."""

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config or ContextConfig()

    def build(self, messages: list[dict[str, Any]], *, workspace: str | Path) -> ContextBuildResult:
        before = serialized_chars(messages)
        transformed: list[dict[str, Any]] = []
        result_ids: list[str] = []
        stale_paths: list[str] = []
        for message in messages:
            rendered, ids, stale = self._bounded_message(message, Path(workspace))
            transformed.append(rendered)
            result_ids.extend(ids)
            stale_paths.extend(stale)

        if serialized_chars(transformed) <= self.config.max_prompt_chars:
            final = transformed
            compacted_count = 0
        else:
            fixed_count = min(2, len(transformed))
            recent_start = max(fixed_count, len(transformed) - self.config.recent_messages)
            early = transformed[fixed_count:recent_start]
            summary = self._summarize(early)
            final = [*transformed[:fixed_count]]
            if early:
                final.append(
                    {
                        "role": "system",
                        "name": "context_summary",
                        "content": summary,
                    }
                )
            final.extend(transformed[recent_start:])
            compacted_count = len(early)

        after = serialized_chars(final)
        return ContextBuildResult(
            messages=final,
            compacted=compacted_count > 0,
            chars_before=before,
            chars_after=after,
            estimated_tokens_before=estimate_tokens(before),
            estimated_tokens_after=estimate_tokens(after),
            compacted_message_count=compacted_count,
            result_ids=sorted(set(result_ids)),
            stale_file_references=sorted(set(stale_paths)),
        )

    def _bounded_message(
        self, message: dict[str, Any], workspace: Path
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        copied = json.loads(json.dumps(message))
        content = copied.get("content")
        if copied.get("role") != "tool" or not isinstance(content, dict):
            return copied, [], []

        stale_paths: list[str] = []
        metadata = content.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get("path"), str):
            expected_hash = metadata.get("content_hash")
            candidate = (workspace / metadata["path"]).resolve(strict=False)
            if (
                expected_hash
                and candidate.is_relative_to(workspace.resolve())
                and candidate.is_file()
                and hashlib.sha256(candidate.read_bytes()).hexdigest() != expected_hash
            ):
                content["output"] = "[stale file observation omitted; read the file again]"
                content["stale"] = True
                stale_paths.append(metadata["path"])

        output = content.get("output")
        if not isinstance(output, str) or len(output) <= self.config.max_tool_output_chars:
            return copied, [], stale_paths
        result_id = hashlib.sha256(output.encode()).hexdigest()
        edge = max(80, self.config.max_tool_output_chars // 2)
        content["output"] = f"{output[:edge]}\n... [full output: {result_id}] ...\n{output[-edge:]}"
        content.setdefault("metadata", {})["result_id"] = result_id
        content["truncated"] = True
        return copied, [result_id], stale_paths

    def _summarize(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        unresolved_errors: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if role == "assistant" and isinstance(content, dict):
                events.append(
                    {
                        "type": content.get("type"),
                        "tool": content.get("tool_name"),
                    }
                )
            elif role == "tool" and isinstance(content, dict):
                event = {
                    "tool": message.get("name"),
                    "success": content.get("success"),
                    "error_type": content.get("error_type"),
                    "changed_paths": (content.get("metadata") or {}).get("changed_paths", []),
                    "result_id": (content.get("metadata") or {}).get("result_id"),
                }
                events.append(event)
                if content.get("success") is False:
                    unresolved_errors.append(event)
        summary = {
            "kind": "compacted_history",
            "source_message_count": len(messages),
            "events": events,
            "unresolved_errors": unresolved_errors,
            "instruction": "Use this summary as history; reread files before relying on stale content.",
        }
        rendered = json.dumps(summary, ensure_ascii=False)
        if len(rendered) > self.config.summary_max_chars:
            summary["events"] = events[-12:]
            summary["summary_truncated"] = True
        return summary
