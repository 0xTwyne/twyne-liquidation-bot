"""
Logging configuration for the liquidation bot.
"""

import logging
import os
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGS_PATH = os.environ.get("LOGS_PATH", "logs/account_monitor_logs.log")


class DetailedExceptionFormatter(logging.Formatter):
    """Formatter that includes full tracebacks for ERROR and above."""

    def __init__(self) -> None:
        super().__init__()
        # No %(exc_info)s placeholder — exc_text is set manually below and
        # appended by the base Formatter, which avoids a literal "None" or
        # double-traceback when exc_info is absent.
        self._detailed = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        self._standard = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        if record.levelno >= logging.ERROR:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            return self._detailed.format(record)
        return self._standard.format(record)


def setup_logger() -> logging.Logger:
    """
    Set up and configure the liquidation bot logger.

    Returns:
        Configured logger instance with console and file handlers.
    """
    logger = logging.getLogger("liquidation_bot")

    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    Path(LOGS_PATH).parent.mkdir(parents=True, exist_ok=True)
    # Rotate at 100 MB, keep 5 backups so the volume-mounted logs dir never
    # grows unboundedly while still retaining ~500 MB of recent history.
    file_handler = RotatingFileHandler(LOGS_PATH, mode="a", maxBytes=100 * 1024 * 1024, backupCount=5)

    formatter = DetailedExceptionFormatter()
    console_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger


def global_exception_handler(exctype: type, value: BaseException, tb: Any) -> None:
    """
    Global exception handler to log uncaught exceptions.

    Args:
        exctype: The type of the exception.
        value: The exception instance.
        tb: A traceback object encapsulating the call stack.
    """
    logger = logging.getLogger("liquidation_bot")
    trace_str = "".join(traceback.format_exception(exctype, value, tb))
    logger.critical("Uncaught exception:\n %s", trace_str)
