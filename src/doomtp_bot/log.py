"""structlog setup: JSON to stdout in containers, pretty console locally."""

from __future__ import annotations

import logging
import re
import sys

import structlog

_SENSITIVE_QUERY = re.compile(r"([?&](?:code|state|access_token|refresh_token)=)[^&\s\"]+")


class RedactQueryFilter(logging.Filter):
    """Mask OAuth codes and tokens in logged URLs (uvicorn access log, library messages)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                _SENSITIVE_QUERY.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        if isinstance(record.msg, str):
            record.msg = _SENSITIVE_QUERY.sub(r"\1[redacted]", record.msg)
        return True


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s")
    redact = RedactQueryFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact)
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if fmt == "json" else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )
