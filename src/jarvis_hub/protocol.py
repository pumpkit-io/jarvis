from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, cast

from jarvis_hub.event_log import Event

MAX_SEQ = (1 << 63) - 1


def json_object(raw: bytes) -> dict[str, Any]:
    if len(raw) > 65536:
        raise ValueError("JSON exceeds 64 KiB")

    def constant(value: str) -> Any:
        raise ValueError("Expected standard JSON")

    value = json.loads(raw.decode("utf-8"), parse_constant=constant)
    # Escaped lone surrogates also violate the wire's UTF-8 contract.
    json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return cast(dict[str, Any], value)


def sequence(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SEQ:
        raise ValueError("Expected a nonnegative int64 sequence")
    return value


def wire_event(event: Event) -> dict[str, Any]:
    return {"version": 1, "seq": event.seq,
            "time": datetime.fromtimestamp(event.time, timezone.utc).isoformat().replace("+00:00", "Z"),
            "type": event.type, "client_id": event.client_id, "payload": event.payload}


def failure(code: str, message: str) -> dict[str, Any]:
    return {"version": 1, "error": {"code": code, "message": message, "retry_after_seconds": None}}
