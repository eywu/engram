"""Structured logging and lightweight runtime telemetry for Engram."""
from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

_CONFIGURED = False


class DailyJSONLogHandler(logging.Handler):
    """Write log records to ~/.engram/logs/engram-YYYY-MM-DD.jsonl."""

    def __init__(self, log_dir: Path):
        super().__init__()
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            today = datetime.datetime.now(datetime.UTC).date().isoformat()
            path = self.log_dir / f"engram-{today}.jsonl"
            with self._lock, path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            self.handleError(record)


def configure_logging(
    log_dir: Path | None = None,
    *,
    level: int = logging.INFO,
    force: bool = False,
) -> None:
    """Configure JSON logs to daily files and stdout.

    Standard-library logs, structlog logs, and claude_agent_sdk.* logs all
    flow through the same processors.
    """
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    log_dir = log_dir or (Path.home() / ".engram" / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    shared_processors = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )

    file_handler = DailyJSONLogHandler(log_dir)
    file_handler.setFormatter(formatter)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [file_handler, stdout_handler]
    root.setLevel(level)

    for logger_name in ("claude_agent_sdk", "claude_agent_sdk._internal"):
        sdk_logger = logging.getLogger(logger_name)
        sdk_logger.setLevel(level)
        sdk_logger.propagate = True

    _CONFIGURED = True


def cli_stderr_logger(channel_id: str) -> Callable[[str], None]:
    """Return an SDK stderr callback that writes non-empty lines to logs."""

    log = structlog.get_logger("engram.agent.stderr").bind(channel_id=channel_id)

    def _callback(line: str) -> None:
        text = line.rstrip()
        if text:
            log.warning("agent.cli_stderr", stderr=text)

    return _callback


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
