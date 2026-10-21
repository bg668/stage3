from __future__ import annotations

import logging
from pathlib import Path

from .config import LoggingConfig

LOGGER_NAME = "bounded_investigator"


def configure_logging(config: LoggingConfig) -> logging.Logger:
    """Configure package logging once for CLI execution.

    The logger intentionally stays compact: progress, stage timing, budget usage,
    revision decisions, and failures. It does not dump prompts or API keys.
    """
    logger = logging.getLogger(LOGGER_NAME)
    level = getattr(logging, config.level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    # CLI commands may initialize logging more than once in tests or embeddings.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if config.console:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(formatter)
        logger.addHandler(console)

    if config.file:
        log_path = Path(config.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger(component: str | None = None) -> logging.Logger:
    if component:
        return logging.getLogger(f"{LOGGER_NAME}.{component}")
    return logging.getLogger(LOGGER_NAME)
