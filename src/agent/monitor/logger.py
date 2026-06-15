"""Structured logging setup. Use `get_logger(__name__)` everywhere; never print()."""
from __future__ import annotations

import logging
import sys

import structlog

_configured = False


def configure(level: int = logging.INFO) -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),  # human-readable; swap to JSONRenderer in prod
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "agent") -> structlog.BoundLogger:
    if not _configured:
        configure()
    return structlog.get_logger(name)
