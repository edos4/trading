"""Structured logging via loguru."""

from loguru import logger

logger.remove()
logger.add(
    lambda msg: print(msg, end=""),
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | {message}\n",
    level="INFO",
)
logger.add(
    "logs/bot.log",
    rotation="10 MB",
    retention="30 days",
    level="DEBUG",
)

log = logger
