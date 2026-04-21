"""Minimal structured telemetry sink for runtime events."""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from pathlib import Path
from typing import Any

from engram import paths

log = logging.getLogger(__name__)
_lock = threading.Lock()


def record_event(
    event: str,
    payload: dict[str, Any],
    *,
    home: Path | None = None,
) -> None:
    """Append one telemetry event to Engram's JSONL telemetry log."""
    record = {
        "ts": dt.datetime.now(dt.UTC).isoformat(),
        "event": event,
        **payload,
    }
    path = paths.log_dir(home) / "telemetry.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), default=str)
    with _lock, path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    log.info("telemetry.%s %s", event, payload)
