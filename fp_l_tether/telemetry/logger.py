"""Structured logging using ``structlog``.

Two parallel sinks:
  - **Human text** on stderr — colored via ``ConsoleRenderer`` when on a TTY.
  - **JSON** to optional ``.jsonl`` file — machine-readable, one event per line.

Usage::

    from fp_l_tether.telemetry import setup_logging, get_logger, log_shot
    setup_logging(cfg.telemetry)
    log = get_logger("daemon")
    log.info("camera_attached", model="fp L", bus=0, addr=1)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import structlog

from fp_l_tether.config import TelemetryConfig


_CONFIGURED = False
_JSON_FH: Any = None


def setup_logging(cfg: TelemetryConfig) -> None:
    """Idempotent global setup — safe to call multiple times."""
    global _CONFIGURED, _JSON_FH
    if _CONFIGURED:
        return

    log_level = getattr(logging, cfg.log_level)

    # JSON sink (per-event jsonl), opened first so we can mirror events to it
    json_emitter = None
    if cfg.json_log_file is not None:
        cfg.json_log_file.parent.mkdir(parents=True, exist_ok=True)
        _JSON_FH = open(cfg.json_log_file, "a", buffering=1, encoding="utf-8")  # noqa: SIM115

        def _emit_json(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
            try:
                # Make a serializable copy (drop Path, datetime → str via default)
                _JSON_FH.write(
                    json.dumps(event_dict, ensure_ascii=False, default=str) + "\n"
                )
            except Exception:  # pragma: no cover
                pass
            return event_dict

        json_emitter = _emit_json

    # Build the processor pipeline. NOTE: do NOT use
    # ``structlog.stdlib.add_logger_name`` here — that requires a stdlib
    # Logger (with .name attribute), and we use PrintLogger.
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if json_emitter is not None:
        # Tee BEFORE the renderer so JSON gets structured data, not formatted text
        processors.append(json_emitter)
    processors.append(structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Optional plain-text mirror (uses stdlib logging behind the scenes)
    if cfg.text_log_file is not None:
        cfg.text_log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(cfg.text_log_file, encoding="utf-8")
        handler.setLevel(log_level)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)-30s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logging.basicConfig(level=log_level, handlers=[handler])
    else:
        # Still configure stdlib root so libusb / pyobjc warnings are visible
        logging.basicConfig(
            format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)-30s %(message)s",
            datefmt="%H:%M:%S",
            level=log_level,
            stream=sys.stderr,
        )

    _CONFIGURED = True


def get_logger(name: str = "fp_l_tether") -> Any:
    """Get a bound logger with ``logger=<name>`` already in context."""
    return structlog.get_logger().bind(logger=name)


# ---------------------------------------------------------------------------
# Domain-specific log helpers
# ---------------------------------------------------------------------------


def log_shot(
    logger: Any,
    *,
    filename: str,
    size: int,
    elapsed_s: float,
    image_id: int = 0,
    slot: int = 0,
    image_db_head: int = 0,
    image_db_tail: int = 0,
    trigger: str = "unknown",
) -> None:
    """One-line summary of a successful shot."""
    mbps = (size / 1024 / 1024) / elapsed_s if elapsed_s > 0 else 0
    logger.info(
        "shot",
        filename=filename,
        size=size,
        size_mb=round(size / 1024 / 1024, 2),
        elapsed_s=round(elapsed_s, 2),
        mbps=round(mbps, 2),
        image_id=image_id,
        slot=slot,
        db_head=image_db_head,
        db_tail=image_db_tail,
        trigger=trigger,
    )
