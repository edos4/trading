"""
main.py — Entry point. Just runs the market scanner.
No webhook server. No external dependencies to set up.

Usage:
    python main.py

Prerequisites:
  - TWS or IB Gateway running locally (paper: 7497, live: 7496)
  - .env file filled in (copy from .env.example)
  - pip install -r requirements.txt
"""

import asyncio
import os

from config import settings
from core.scanner import MarketScanner
from utils.logger import log


async def main():
    os.makedirs("logs",   exist_ok=True)
    os.makedirs("charts", exist_ok=True)

    log.info("=" * 60)
    log.info(f"  Trading Bot — mode: {settings.trading_mode.upper()}")
    log.info(f"  Watchlist:  {settings.symbols}")
    log.info(f"  Scan every: {settings.scan_interval_seconds}s")
    log.info(f"  History:    {settings.tv_history_days} daily bars")
    log.info(f"  Vision:     {'ON' if settings.vision_confirmation_enabled else 'OFF'}")
    log.info(f"  IBKR:       disabled (commented out)")
    log.info("=" * 60)

    if settings.is_live:
        log.warning("⚠️  LIVE TRADING MODE — real capital is at risk")

    scanner = MarketScanner()
    await scanner.run()


if __name__ == "__main__":
    asyncio.run(main())
