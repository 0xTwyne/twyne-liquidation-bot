"""
Logging configuration for the liquidation bot.
"""

import logging
import os
import traceback
from pathlib import Path
from typing import Any

LOGS_PATH = os.environ.get("LOGS_PATH", "logs/account_monitor_logs.log")


class DetailedExceptionFormatter(logging.Formatter):
    """Formatter that includes full tracebacks for ERROR and above."""

    def __init__(self) -> None:
        super().__init__()
        self._detailed = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s\n%(exc_info)s")
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
    file_handler = logging.FileHandler(LOGS_PATH, mode="a")

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
