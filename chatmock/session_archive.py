from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

from .utils import get_home_dir


@dataclass
class PendingResponsesTurn:
    session_id: str
    transport: str
    model: str | None
    instructions: str | None
    input_delta: List[Dict[str, Any]]
    is_follow_up: bool
    previous_response_id: str | None
    explicit_previous_response_id: bool
    output_items: List[Dict[str, Any]] = field(default_factory=list)
    response_id: str | None = None
    finished: bool = False


def create_pending_responses_turn(prepared: Any, *, transport: str) -> PendingResponsesTurn:
    return PendingResponsesTurn(
        session_id=prepared.session_id,
        transport=transport,
        model=prepared.full_payload.get("model") if isinstance(prepared.full_payload, dict) and isinstance(prepared.full_payload.get("model"), str) else None,
        instructions=prepared.full_payload.get("instructions") if isinstance(prepared.full_payload, dict) and isinstance(prepared.full_payload.get("instructions"), str) else None,
        input_delta=[copy.deepcopy(item) for item in prepared.input_delta],
        is_follow_up=bool(prepared.is_follow_up),
        previous_response_id=prepared.previous_response_id,
        explicit_previous_response_id=bool(prepared.explicit_previous_response_id),
    )


def note_responses_turn_event(pending: PendingResponsesTurn | None, event: Dict[str, Any]) -> None:
    if pending is None or pending.finished or not isinstance(event, dict):
        return
    kind = event.get("type")
    if kind == "response.created":
        response = event.get("response")
        if isinstance(response, dict) and isinstance(response.get("id"), str):
            pending.response_id = response.get("id")
        return
    if kind == "response.output_item.done":
        item = event.get("item")
        if isinstance(item, dict):
            pending.output_items.append(copy.deepcopy(item))
        return
    if kind == "response.completed":
        response = event.get("response")
        if isinstance(response, dict):
            if isinstance(response.get("id"), str):
                pending.response_id = response.get("id")
            output = response.get("output")
            if isinstance(output, list) and output:
                pending.output_items = [copy.deepcopy(item) for item in output if isinstance(item, dict)]
        _append_record(
            pending,
            {
                "status": "completed",
                "response_id": pending.response_id,
                "output_delta": [copy.deepcopy(item) for item in pending.output_items],
            },
        )
        return
    if kind in ("response.failed", "error"):
        _append_record(
            pending,
            {
                "status": "failed",
                "error": copy.deepcopy(event),
            },
        )


def append_responses_turn_success(pending: PendingResponsesTurn | None, response_obj: Dict[str, Any]) -> None:
    if pending is None or pending.finished or not isinstance(response_obj, dict):
        return
    if isinstance(response_obj.get("id"), str):
        pending.response_id = response_obj.get("id")
    output = response_obj.get("output")
    if isinstance(output, list) and output:
        pending.output_items = [copy.deepcopy(item) for item in output if isinstance(item, dict)]
    _append_record(
        pending,
        {
            "status": "completed",
            "response_id": pending.response_id,
            "output_delta": [copy.deepcopy(item) for item in pending.output_items],
        },
    )


def append_responses_turn_failure(pending: PendingResponsesTurn | None, error: Any) -> None:
    if pending is None or pending.finished:
        return
    _append_record(
        pending,
        {
            "status": "failed",
            "error": copy.deepcopy(error),
        },
    )


def _append_record(pending: PendingResponsesTurn, payload: Dict[str, Any]) -> None:
    path = _session_log_path(pending.session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": pending.session_id,
        "transport": pending.transport,
        "model": pending.model,
        "instructions": pending.instructions,
        "is_follow_up": pending.is_follow_up,
        "previous_response_id": pending.previous_response_id,
        "explicit_previous_response_id": pending.explicit_previous_response_id,
        "input_delta": [copy.deepcopy(item) for item in pending.input_delta],
    }
    record.update(payload)
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    pending.finished = True


def _session_log_path(session_id: str) -> str:
    safe_session_id = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in session_id)
    return os.path.join(get_home_dir(), "sessions", f"{safe_session_id}.jsonl")
