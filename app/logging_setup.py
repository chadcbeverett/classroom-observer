"""Structured JSON logging, opt-in via env.

Set ``OBSERVER_LOG_FORMAT=json`` to route every log line through a
JSON formatter that emits one object per line — a log-aggregator's
preferred shape (Datadog, CloudWatch, Loki, Splunk).

Default (env unset or set to anything else) preserves uvicorn's
human-readable format so local dev stays readable.

No third-party deps — the formatter is a small stdlib ``logging``
Formatter subclass.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record.

    Fields:
      ts        ISO-8601 UTC timestamp
      level     level name (INFO / WARNING / ERROR / etc.)
      logger    logger name (e.g. 'uvicorn.error', 'app.smtp_sender')
      msg       the formatted message
      exc       exception traceback as string (only when logging.exception)
      extra_*   any extra fields the caller passed via `extra={}`

    Deliberately does NOT log the process id / thread id / file / line
    by default — those add noise for a coach-scale service. Add them via
    the ``extra`` kwarg when you actually need them.
    """

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
                          .isoformat(timespec="milliseconds")
                          .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        # Any extras — the caller's `extra={...}` shows up as attrs on the
        # record. Skip anything from the reserved-set to avoid double-fields.
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)  # ensure it serializes
                obj[key] = value
            except (TypeError, ValueError):
                obj[key] = repr(value)
        return json.dumps(obj, ensure_ascii=False)


def configure() -> None:
    """Wire the JSON formatter into every logger uvicorn uses when
    ``OBSERVER_LOG_FORMAT=json`` is set. Idempotent — called from
    _bootstrap; safe on reload.

    Rewrites the formatter on every handler of the root logger and the
    named uvicorn loggers. Uvicorn's `logconfig_dict` would be cleaner
    if we owned the process boot, but we don't (uvicorn is the process
    entry point here), so we retrofit after `uvicorn` set its own
    handlers up.
    """
    if os.environ.get("OBSERVER_LOG_FORMAT", "").lower() != "json":
        return
    fmt = JsonFormatter()
    for logger_name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        for handler in logger.handlers:
            handler.setFormatter(fmt)
