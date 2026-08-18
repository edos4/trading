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

    # ── Paper trade stream (replays historical CSVs when markets are closed) ──
    papertrade_stream_dir: str = "/home/r00t/stocks_data"
    papertrade_stream_host: str = "127.0.0.1"
    papertrade_stream_port: int = 8765
    # Warm enough for Kronos gate LOOKBACK=400 when replaying near CSV end.
    papertrade_stream_lookback_bars: int = 420
    # Legacy pacing knob retained for .env compatibility. Replay advancement
    # is now scanner-controlled and happens once per completed universe scan,
    # so scan duration can never desynchronize symbols.
    papertrade_stream_interval_seconds: int = 60
    # YYYY-MM-DD cursor start for CSV replay. None = near end of each CSV
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
    # After a chart pattern fires, require Kronos 1w forecast to agree on
    # direction and clear kronos_min_move_pct. Not a standalone entry pattern —
    # veto/confirm layer only (not the Kronos finetune top-K strategy).
    kronos_min_move_pct: float = 0.06
    kronos_sample_count: int = 3
    kronos_gate_enabled: bool = True
    # Safety default: an enabled Kronos gate must not silently disappear if
    # its model/data path is unavailable, so it is fail-closed (rejects the
    # signal). Set true only for research runs.
    kronos_gate_fail_open: bool = False
    # Load finetuned weights from ~/Kronos/finetuned when present; else base.
    kronos_use_finetuned: bool = False
    # Off by default: rewriting pattern TP/SL from the 1w forecast made paper
    # and "pattern" backtests describe different strategies (paper +81% while
    # Kronos-on formal BT printed PF 0.046). Gate still vetoes on direction;
    # exits stay the pattern's until adjust_exits is proven to lift expectancy.
    kronos_gate_adjust_exits: bool = False

    # ── Kronos ranked forecast sleeve (core/kronos_rank_sleeve.py) ──────────
    # Independent entry source beside Toby patterns: cross-sectionally rank
    # predicted 1w returns and take top_k longs / bottom_k shorts. Off by
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
    # (Kronos 6% already passed every 003 loser). Fail-open on short history.
    volume_gate_enabled: bool = True
    volume_gate_rvol_min: float = 1.5
    volume_gate_obv_bars: int = 5

    # ── Vision ────────────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    vision_confirmation_enabled: bool = False
    vision_min_indicator_confidence: float = 0.6

    # ── Notifications ──────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── PostgreSQL (stock history DB, python main.py --ingest-db/--check-db) ─
    # Local socket + peer auth (OS user r00t → role r00t), no password.
    # Discrete fallbacks below are used only when DATABASE_URL is empty.
    database_url: str = "postgresql://r00t@/stocks_history?host=/var/run/postgresql"
    db_host: str = "/var/run/postgresql"
    db_port: int = 5432
    db_name: str = "stocks_history"
    db_user: str = "r00t"
    db_password: str = ""

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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

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
        return max(1, min(value, 512))

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
#   pattern_002_double_top / pattern_009_flag_pattern / pattern_010_pennant —
#   previously isolated as weak; stay off.
#
#   pattern_005_rounding_top / pattern_006_upward_channel /
#   pattern_007_descending_channel / pattern_008_head_and_shoulders —
#   historically net-negative. Stay off.
#
#   pattern_003_double_bottom stays ON with neckline-break-only entry
#   (2026-08-17 US paper: 79 day-7-without-break fills, PF 0.29).
DISABLED_PATTERNS: list[str] = [
    "pattern_011_breakout_retest",
    "pattern_012_ml_signal",
    "pattern_009_flag_pattern",
    "pattern_010_pennant",
    "pattern_002_double_top",
    "pattern_005_rounding_top",
    "pattern_006_upward_channel",
    "pattern_007_descending_channel",
    "pattern_008_head_and_shoulders",
]