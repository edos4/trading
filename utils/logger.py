"""Structured logging via loguru."""

import multiprocessing as mp

from loguru import logger

logger.remove()
_CONSOLE_HANDLER_ID = logger.add(
    lambda msg: print(msg, end=""),
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | {message}\n",
    level="INFO",
)

# Backtest process-pool workers (spawn context) re-import this module fresh,
# so each worker used to open its own handle onto logs/bot.log — many
# processes rotating/writing the same file concurrently, which slowed the
# backtest down and crashed rotation with FileNotFoundError when one worker
# renamed the file out from under another. Only the main process owns the
# file sink; workers keep the console handler only.
if mp.parent_process() is None:
    logger.add(
        "logs/bot.log",
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
    )


def set_console_level(level: str | int) -> None:
    """Dynamically change the console handler's minimum log level."""
    logger.remove(_CONSOLE_HANDLER_ID)
    logger.add(
        lambda msg: print(msg, end=""),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | {message}\n",
        level=level,
    )


log = logger
