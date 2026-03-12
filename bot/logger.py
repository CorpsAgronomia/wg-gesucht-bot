from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": getattr(record, "event", record.getMessage()),
            "status": getattr(
                record,
                "status",
                "error" if record.levelno >= logging.ERROR else "success",
            ),
            "error": getattr(record, "error", None),
            "level": record.levelname,
            "component": getattr(record, "component", "app"),
            "message": record.getMessage(),
        }

        details = getattr(record, "details", None)
        if details:
            payload["details"] = details

        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=True)


def configure_logging(logs_dir: Path) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wg_bump_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = JsonFormatter()

    info_handler = RotatingFileHandler(
        logs_dir / "bot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)

    error_handler = RotatingFileHandler(
        logs_dir / "errors.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    logger.addHandler(info_handler)
    logger.addHandler(error_handler)
    logger.addHandler(stream_handler)

    return logger


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    status: str,
    component: str = "app",
    level: int = logging.INFO,
    error: str | None = None,
    **details: Any,
) -> None:
    logger.log(
        level,
        event,
        extra={
            "event": event,
            "status": status,
            "component": component,
            "error": error,
            "details": details or None,
        },
    )


def shutdown_logging() -> None:
    logging.shutdown()
