#!/usr/bin/env python3
# encoding=utf-8
"""
Helper utilities for the Netatmo Client.
"""

import logging


def configure_logging(logger: logging.Logger, level: str = "INFO") -> logging.Logger:
    """
    Configure logging for the application and all modules.

    This function configures a logger with a consistent format
    and level. If the logger already has handlers, it will not add duplicates.

    Args:
        level: Default logging level name (e.g., "INFO", "DEBUG", "WARNING")
        logger: Optional logger instance to configure. If None, a new logger is created.

    Returns:
        The configured logger instance
    """
    # Only add handler if logger doesn't already have one
    if not logger.handlers:
        _fmt = logging.Formatter(
            "%(asctime)s - %(module)s:%(lineno)d - %(levelname)s:%(message)s", datefmt="%d.%m.%Y %H:%M:%S"
        )

        _ch = logging.StreamHandler()
        _ch.setFormatter(_fmt)

        logger.addHandler(_ch)

        logger.setLevel(level)
        logger.info(f"Setting loglevel to {level} for {logger}.")

    return logger
