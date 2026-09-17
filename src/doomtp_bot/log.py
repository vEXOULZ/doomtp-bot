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
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(a) for a in record.args)
        elif isinstance(record.args, dict):  # "%(name)s"-style args must stay a mapping
            record.args = {k: _redact(v) for k, v in record.args.items()}
        record.msg = _redact(record.msg)
        return True


def _redact(value: object) -> object:
    return _SENSITIVE_QUERY.sub(r"\1[redacted]", value) if isinstance(value, str) else value


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
