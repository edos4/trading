"""
config.py — All settings loaded from .env.
Import `settings` everywhere; nothing reads os.environ directly.
"""

from pydantic_settings import BaseSettings
from pydantic import field_validator
from enum import Enum


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    # ── IBKR ──────────────────────────────────────────────────────────────
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1

    # ── Bot behaviour ──────────────────────────────────────────────────────
    # Swing trading: fewer, larger, longer-held positions rather than many
    # small intraday ones — sizing and exposure limits reflect that.
    trading_mode: TradingMode = TradingMode.PAPER
    max_position_size_usd: float = 3000.0
    max_daily_loss_usd: float = 1500.0
    max_open_positions: int = 8

    # ── Scanner ────────────────────────────────────────────────────────────
    watchlist: str
    tv_screener: str
    tv_exchange: str
    tv_exchange_overrides: str = ""
    tv_use_ta_fallback: bool = False  # unused; kept for .env compatibility
    # Daily bars to pull from TradingView screener (close[0]=today, close[1]=yesterday, …)
    tv_history_days: int = 252  # ~1 trading year
    # Swing setups form on daily/weekly bars, which only print one new candle
    # per day/week — no need to poll every minute. Once per hour is plenty
    # and keeps TradingView/API call volume low.
    scan_interval_seconds: int = 3600
    # How many symbols to process concurrently during each scan cycle.
    # Each concurrent worker opens its own MCP session.
    scanner_concurrency: int = 10

    # ── Vision ────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    vision_confirmation_enabled: bool = False
    vision_min_indicator_confidence: float = 0.6

    # ── Notifications ──────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("tv_history_days")
    @classmethod
    def _clamp_history_days(cls, value: int) -> int:
        return max(1, min(value, 365))

    @property
    def is_live(self) -> bool:
        return self.trading_mode == TradingMode.LIVE

    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

    @property
    def symbol_exchange_overrides(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in self.tv_exchange_overrides.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            symbol, exchange = pair.split(":", 1)
            symbol = symbol.strip().upper()
            exchange = exchange.strip().upper()
            if symbol and exchange:
                out[symbol] = exchange
        return out


settings = Settings()
