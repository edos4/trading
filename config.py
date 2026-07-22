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
    max_daily_loss_usd: float = 1500.0
    max_open_positions: int = 0  # <=0 means unlimited

    # ── Paper trading ─────────────────────────────────────────────────────
    paper_initial_capital: float = 100_000.0
    paper_slippage_pct: float = 0.0005

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

    # ── ML signal (pattern_012_ml_signal, trained via `main.py --learn`) ────
    # Trade-defining params (horizon/target/stop) live in the trained model's
    # meta.json, not here — this is only the inference-time confidence gate.
    ml_confidence_threshold: float = 0.6

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

# pattern_009_flag_pattern (28% win, -18.1% total) and
# pattern_006_upward_channel (0% win, -13.1% total) were net negative over a
# 162-trade / 7-pattern backtest. Disabled by default everywhere a scanner
# runs unattended (backtest aggregate run, paper trading). Still testable in
# isolation via --pattern. Caveat: upward_channel's sample was only 7 trades —
# revisit if a larger sample says otherwise.
#
# pattern_007_descending_channel (n=6, pf=0.36) and pattern_008_head_and_shoulders
# (n=4, pf=0.02) are too small a sample to trust either way but currently lose
# money live. pattern_012_ml_signal (n=3, pf=7826) is the opposite problem —
# too few trades for that PF to mean anything, not a real edge yet. All three
# disabled until sample size grows; revisit via --pattern in isolation.
#
# pattern_011_breakout_retest: net negative over a statistically meaningful
# 76-trade sample (pf=0.75, pnl=-5.69%) in the same backtest run. Its own
# docstring calls it a "DRAFT ruleset ... NOT backtested" — that draft status
# now has a real backtest verdict against it. Disabled until the entry/exit
# rules are reworked and re-tested in isolation via --pattern.
DISABLED_PATTERNS: list[str] = [
    "pattern_009_flag_pattern",
    "pattern_006_upward_channel",
    "pattern_007_descending_channel",
    "pattern_008_head_and_shoulders",
    "pattern_012_ml_signal",
    "pattern_011_breakout_retest",
]
