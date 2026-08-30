"""
config.py — All settings loaded from .env.
Import `settings` everywhere; nothing reads os.environ directly.
"""

from pydantic_settings import BaseSettings
from pydantic import field_validator
from enum import Enum

# Daily bars each symbol must carry into a pattern scan so forming setups
# (not only today's exact trigger bar) can be recognized.
PATTERN_SCAN_HISTORY_BARS = 30


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
    # us = NASDAQ/NYSE USD book; ph = PSE PHP long-only daytime book.
    # UI/web can still pick a market per backtest/paper run.
    market: str = "us"
    max_daily_loss_usd: float = 1500.0
    max_daily_loss_php: float = 15000.0
    max_open_positions: int = 8  # <=0 means unlimited
    # Cap concurrent seats in any one pattern so the book cannot collapse
    # into a single setup (paper was 8/10 descending-channel). 0 = unlimited.
    max_open_per_pattern: int = 4
    # Skip leftover crumbs after risk/exposure caps (e.g. BUY 1 share).
    min_position_notional: float = 1000.0

    # ── Paper trading ─────────────────────────────────────────────────────
    paper_initial_capital: float = 100_000.0
    paper_slippage_pct: float = 0.0005

    # ── Paper trade stream (replays stocks_history when markets are closed) ──
    papertrade_stream_host: str = "127.0.0.1"
    papertrade_stream_port: int = 8765
    # Warm enough for Kronos gate LOOKBACK=400 when replaying near tape end.
    papertrade_stream_lookback_bars: int = 420
    # Legacy pacing knob retained for .env compatibility. Replay advancement
    # is now scanner-controlled and happens once per completed universe scan,
    # so scan duration can never desynchronize symbols.
    papertrade_stream_interval_seconds: int = 60
    # YYYY-MM-DD cursor start for stream replay. None = near end of each tape
    # (last papertrade_stream_lookback_bars). UI datepicker overrides this
    # when launching via "Use paper trade stream".
    papertrade_stream_start_date: str | None = None

    # ── Scanner ────────────────────────────────────────────────────────────
    watchlist: str
    tv_screener: str
    tv_exchange: str
    tv_exchange_overrides: str = ""
    tv_use_ta_fallback: bool = False  # unused; kept for .env compatibility
    # Daily bars to pull from Yahoo chart + screener overlay. Sized for Kronos
    # gate LOOKBACK=400 (official demo) plus a little headroom; clamp ≤512.
    # Floor is PATTERN_SCAN_HISTORY_BARS so every pattern scan has a month of
    # prior closes (forming setups), not just the latest bar.
    tv_history_days: int = 450
    # Swing setups form on daily/weekly bars, which only print one new candle
    # per day/week — no need to poll every minute. Once per hour is plenty
    # and keeps TradingView/API call volume low.
    scan_interval_seconds: int = 3600
    # How many symbols to process concurrently during each scan cycle.
    # Each concurrent worker opens its own MCP session. Screener POSTs are
    # still paced by tv_screener_min_interval_seconds across all workers.
    scanner_concurrency: int = 15
    # Min gap between TradingView scanner POSTs (america/scan). Undocumented
    # IP limit — ~10+ req/s trips HTTP 429 mid-universe. 0.5s ≈ 2 req/s.
    tv_screener_min_interval_seconds: float = 0.5
    # Retries after HTTP 429 from scanner.tradingview.com (exponential backoff).
    tv_screener_max_retries: int = 5
    tv_screener_retry_backoff_seconds: float = 2.0

    # ── ML signal (pattern_012_ml_signal, trained via `main.py --learn`) ────
    # Trade-defining params (horizon/target/stop) live in the trained model's
    # meta.json, not here — this is only the inference-time confidence gate.
    ml_confidence_threshold: float = 0.6

    # ── Kronos confirm gate (core/kronos_gate.py) ───────────────────────────
    # After a chart pattern fires, require Kronos 3-trading-day forecast to agree
    # on direction and clear 3% in those 3 days (kronos_min_move_pct). Not a
    # standalone entry pattern — veto/confirm layer only (not the Kronos
    # finetune top-K strategy).
    kronos_min_move_pct: float = 0.03
    kronos_sample_count: int = 3
    kronos_gate_enabled: bool = True
    # Safety default: an enabled Kronos gate must not silently disappear if
    # its model/data path is unavailable, so it is fail-closed (rejects the
    # signal). Set true only for research runs.
    kronos_gate_fail_open: bool = False
    # Load finetuned weights from ~/Kronos/finetuned when present; else base.
    kronos_use_finetuned: bool = False
    # Off by default: rewriting pattern TP/SL from the 3d forecast made paper
    # and "pattern" backtests describe different strategies (paper +81% while
    # Kronos-on formal BT printed PF 0.046). Gate still vetoes on direction;
    # exits stay the pattern's until adjust_exits is proven to lift expectancy.
    kronos_gate_adjust_exits: bool = False
    # Collect pattern hits, then one KronosPredictor.predict_batch. Off by
    # default — UI/web "Batch Kronos" checkbox opts in when Kronos is on.
    kronos_batch_enabled: bool = False
    # Series per predict_batch call on CUDA. GPU batch = this × sample_count.
    # No CUDA: runtime caps at 4 (see effective_kronos_batch_size).
    kronos_batch_size: int = 16

    # ── Kronos ranked forecast sleeve (core/kronos_rank_sleeve.py) ──────────
    # Independent entry source beside Toby patterns: cross-sectionally rank
    # predicted 3-trading-day returns and take top_k longs / bottom_k shorts. Off by
    # default (GPU cost per scan + needs BT validation). Does not replace
    # kronos_gate — gate still filters chart-pattern signals only.
    kronos_rank_enabled: bool = False
    kronos_rank_top_k: int = 3
    kronos_rank_bottom_k: int = 3
    kronos_rank_long_only: bool = True
    # None → reuse kronos_min_move_pct as the |pred| floor for sleeve entries.
    kronos_rank_min_move_pct: float | None = None
    # Bars between cross-sectional re-ranks in backtest (5 ≈ weekly).
    kronos_rank_rebalance_bars: int = 5

    # ── Volume confirm gate (analysis/price_volume.py) ─────────────────────
    # After a chart pattern fires, require relative volume (signal bar /
    # SMA20) ≥ volume_gate_rvol_min AND OBV slope over volume_gate_obv_bars
    # to agree with BUY/SELL. On for paper/scan after the 2026-08-17 US book
    # (Kronos 3% already passed every 003 loser). Fail-open on short history.
    volume_gate_enabled: bool = True
    volume_gate_rvol_min: float = 2.0
    volume_gate_obv_bars: int = 5

    # ── Vision ────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    vision_confirmation_enabled: bool = False
    vision_min_indicator_confidence: float = 0.6

    # ── Notifications ──────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── PostgreSQL (stock history DB, python main.py --check-db/--update-db) ─
    # Local socket + peer auth (OS user r00t → role r00t), no password.
    # Discrete fallbacks below are used only when DATABASE_URL is empty.
    database_url: str = "postgresql://r00t@/stocks_history?host=/var/run/postgresql"
    db_host: str = "/var/run/postgresql"
    db_port: int = 5432
    db_name: str = "stocks_history"
    db_user: str = "r00t"
    db_password: str = ""

    # ── Remote stocks_history API (local --ui / --web / Kronos) ─────────────
    # Local --ui/--web auto-use https://33ai.edos.uk when this is empty (charts
    # included; no Yahoo). All OHLCV reads use this API, never local Postgres.
    # VPS --web must leave this empty and set owner so it still writes Postgres
    # via --update-db and serves GET /api/history.
    stocks_history_url: str = ""
    stocks_history_username: str = ""
    stocks_history_password: str = ""
    stocks_history_owner: bool = False

    # ── Web UI (python main.py --web) ───────────────────────────────────────
    # Session login. WEB_UI_PASSWORD is required — the server refuses to bind
    # if it is empty so an open VPS dashboard cannot ship by accident.
    web_ui_host: str = "0.0.0.0"
    web_ui_port: int = 8080
    web_ui_username: str = "admin"
    web_ui_password: str = ""
    # Signing key for session cookies. If empty, derived from password at boot
    # (set an explicit long random string in production).
    web_ui_secret_key: str = ""
    # Set true behind HTTPS so the session cookie gets the Secure flag.
    web_ui_https: bool = False
    # Idle session lifetime (hours).
    web_ui_session_hours: int = 12

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @field_validator("market")
    @classmethod
    def _normalize_market(cls, value: str) -> str:
        key = (value or "us").strip().lower()
        if key in ("ph", "pse", "philippines", "philippine"):
            return "ph"
        return "us"

    @field_validator("tv_history_days")
    @classmethod
    def _clamp_history_days(cls, value: int) -> int:
        # Kronos-base max_context is 512; more bars than that are truncated.
        return max(PATTERN_SCAN_HISTORY_BARS, min(value, 512))

    @field_validator("papertrade_stream_lookback_bars")
    @classmethod
    def _clamp_stream_lookback(cls, value: int) -> int:
        return max(PATTERN_SCAN_HISTORY_BARS, int(value))

    @field_validator("tv_screener_min_interval_seconds")
    @classmethod
    def _clamp_screener_interval(cls, value: float) -> float:
        return max(0.0, value)

    @field_validator("tv_screener_max_retries")
    @classmethod
    def _clamp_screener_retries(cls, value: int) -> int:
        return max(0, min(value, 20))

    @field_validator("tv_screener_retry_backoff_seconds")
    @classmethod
    def _clamp_screener_backoff(cls, value: float) -> float:
        return max(0.1, value)

    @field_validator("kronos_batch_size")
    @classmethod
    def _clamp_kronos_batch_size(cls, value: int) -> int:
        return max(1, min(int(value), 256))

    @property
    def stocks_history_auth(self) -> tuple[str, str]:
        """Basic auth for GET /api/history. Password defaults to WEB_UI_USERNAME."""
        user = (self.stocks_history_username or self.web_ui_username or "").strip()
        password = (self.stocks_history_password or self.web_ui_username or "").strip()
        return user, password

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

# Patterns disabled by default everywhere a scanner runs unattended (backtest
# aggregate run, paper trading). Each is still testable in isolation via
# --pattern (an explicit pattern_filter always wins over this list).
#
#   pattern_011_breakout_retest — net negative over a statistically meaningful
#   76-trade sample (pf=0.75, pnl=-5.69%). Its own docstring calls it a "DRAFT
#   ruleset ... NOT backtested"; that draft status now has a real verdict
#   against it. Re-enable only after the entry/exit rules are reworked.
#
#   pattern_012_ml_signal — n=3, pf=7826: too few trades for that PF to mean
#   anything, not a real edge yet. Disabled until the sample grows.
#
#   pattern_002_double_top / pattern_008_head_and_shoulders — 2026-08-30 US
#   paper: 10 shorts, 0 wins, −$3,550 realized (002 also hosted the
#   grind-to-6%-stop cluster). Isolation via --pattern still works.
#
#   pattern_009_flag_pattern / pattern_010_pennant — previously isolated as
#   weak; 009 stays off. 010 is not in this list (unused in the current
#   paper book).
#
#   pattern_006_upward_channel — historically net-negative. Stay off.
#
#   pattern_007_descending_channel stays ON: it is the live long sleeve.
#   2026-08-30 P&L is lottery-dependent (GP); exit/cooldown fixes below
#   address grinders without collapsing trade count.
#
#   pattern_003_double_bottom stays ON with neckline-break-only entry
#   (2026-08-17 US paper: 79 day-7-without-break fills, PF 0.29).
#
#   pattern_004_rounding_bottom — n=3, 0% win, pf=0.00 on 2026-08-18 US
#   paper (−14% equal-weight). Disabled until the saucer rules are reworked.
DISABLED_PATTERNS: list[str] = [
    "pattern_011_breakout_retest",
    "pattern_012_ml_signal",
    "pattern_009_flag_pattern",
    "pattern_006_upward_channel",
    "pattern_002_double_top",
    "pattern_008_head_and_shoulders",
]