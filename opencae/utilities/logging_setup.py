"""Application-wide logging configuration."""
from __future__ import annotations

import logging
import os
from pathlib import Path

LOGGER_NAME = "opencae"


def log_dir() -> Path:
    base = os.environ.get("OPENCAE_HOME") or str(Path.home() / ".opencae")
    p = Path(base) / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def setup_logging(level: int = logging.INFO, to_file: bool = True) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if to_file:
        try:
            fh = logging.FileHandler(log_dir() / "opencae.log", encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:  # pragma: no cover - read-only home etc.
            pass
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
