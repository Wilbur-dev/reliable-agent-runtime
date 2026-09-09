import json
import logging

from runtime.observability import JsonEventLogger


def test_json_event_logger_emits_structured_record(caplog) -> None:
    caplog.set_level(logging.INFO, logger="test.runtime")
    JsonEventLogger(logging.getLogger("test.runtime")).emit(
        "tool_finished",
        task_id="task-1",
        step_sequence=3,
        tool_name="read_file",
        success=True,
    )

    event = json.loads(caplog.records[0].message)
    assert event == {
        "event_type": "tool_finished",
        "step_sequence": 3,
        "success": True,
        "task_id": "task-1",
        "tool_name": "read_file",
    }
