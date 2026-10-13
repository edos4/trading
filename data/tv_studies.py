"""Logged-in TradingView chart session for indicator-on-indicator studies.

Auth is the subscribed account's ``sessionid`` and ``sessionid_sign`` cookies.
Empty cookies disable the client. The chart socket is the same one the website
uses (``prodata``); a child study names its parent study id and plot index in
the source input.
"""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator
from urllib.parse import quote

import httpx
import pandas as pd

from config import settings
from utils.logger import log

_ORIGIN = "https://www.tradingview.com"
_CHART_PAGE = "https://www.tradingview.com/chart/"
_PINE = "https://pine-facade.tradingview.com/pine-facade"
_WS = "wss://prodata.tradingview.com/socket.io/websocket?from=chart&type=chart"
_UA = "trading-bot/2.0"
PRICE_SOURCES = ("open", "high", "low", "close", "hl2", "hlc3", "ohlc4")
_HEADER = "~m~"
_HEARTBEAT = "~h~"

_client: "TvStudyClient | None" = None
_client_lock = threading.Lock()


class TvStudyError(Exception):
    """A study fetch failed for one symbol. ``kind`` is disabled, expired,
    study_not_auth, rate_limit, or error. Never carries a cookie or token.
    """

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


def get_client() -> "TvStudyClient":
    global _client
    with _client_lock:
        if _client is None:
            _client = TvStudyClient()
        return _client


def js_len(text: str) -> int:
    """UTF-16 code units, which is what TradingView's frame length counts."""
    return len(text.encode("utf-16-le")) // 2


def encode_frame(payload: str | dict) -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    return f"{_HEADER}{js_len(body)}{_HEADER}{body}"


def encode_packet(method: str, params: list) -> str:
    return encode_frame({"m": method, "p": params})


def decode_frames(raw: str) -> list[dict]:
    frames: list[dict] = []
    position = 0
    while position < len(raw):
        if not raw.startswith(_HEADER, position):
            break
        length_start = position + len(_HEADER)
        length_end = raw.find(_HEADER, length_start)
        if length_end < 0:
            break
        length = int(raw[length_start:length_end])
        start = length_end + len(_HEADER)
        body = raw[start:start + length]
        position = start + length
        if body.startswith(_HEARTBEAT):
            frames.append({"type": "heartbeat", "id": int(body[len(_HEARTBEAT):])})
            continue
        data = json.loads(body)
        if isinstance(data, dict) and isinstance(data.get("m"), str):
            frames.append({"type": "packet", "packet": data})
        else:
            frames.append({"type": "data", "data": data})
    return frames


def _match(pattern: str, page: str) -> str:
    found = re.search(pattern, page)
    return found.group(1) if found else ""


def parse_user_page(page: str) -> dict[str, str]:
    return {
        "auth_token": _match(r'"auth_token":"(.*?)"', page),
        "plan": _match(r'"pro_plan":"(.*?)"', page),
    }


def child_study_count(studies: list[dict]) -> int:
    """Studies whose source is an earlier study. The first study cannot be one."""
    count = 0
    for index, study in enumerate(studies):
        source = study.get("source") or {}
        if source.get("type") != "study":
            continue
        if index == 0:
            raise TvStudyError("error", "The first study must use a price source.")
        parent = int(source.get("study_index", -1))
        if parent < 0 or parent >= index:
            raise TvStudyError("error", "A study source must point at an earlier study.")
        count += 1
    return count


def _input_value(item: dict) -> Any:
    if "value" in item:
        return item["value"]
    return item.get("defval")


def parse_indicator_definition(payload: dict, study_id: str, version: str) -> dict:
    if not payload.get("success") or not (payload.get("result") or {}).get("metaInfo"):
        reason = payload.get("reason") or "unsupported indicator"
        raise TvStudyError("study_not_auth", f"This account cannot open {study_id}: {reason}")
    meta = payload["result"]["metaInfo"]
    inputs: dict[str, dict] = {}
    for item in meta.get("inputs") or []:
        item_id = str(item.get("id") or "")
        if item_id in ("text", "pineId", "pineVersion"):
            continue
        inputs[item_id] = {
            "name": item.get("name") or item_id,
            "type": item.get("type") or "float",
            "value": _input_value(item),
            "is_fake": bool(item.get("isFake")),
        }
    plots: dict[str, str] = {}
    for plot_id, style in (meta.get("styles") or {}).items():
        plots[str(plot_id)] = str((style or {}).get("title") or plot_id)
    kind = "StrategyScript@tv-scripting-101!" if meta.get("isStrategy") else "Script@tv-scripting-101!"
    pine = meta.get("pine") or {}
    return {
        "id": meta.get("scriptIdPart") or study_id,
        "version": str(pine.get("version") or version or "last"),
        "description": meta.get("description") or study_id,
        "type": kind,
        "inputs": inputs,
        "plots": plots,
        "script": payload["result"].get("ilTemplate") or "",
    }


def plot_index(definition: dict, plot: str) -> int:
    if plot.startswith("plot_") and plot[5:].isdigit():
        return int(plot[5:])
    keys = list(definition.get("plots") or {})
    if plot in keys:
        return keys.index(plot)
    return 0


def study_inputs(definition: dict, overrides: dict, source_value: str) -> dict:
    inputs: dict[str, Any] = {
        "text": definition.get("script") or "",
        "pineId": definition["id"],
        "pineVersion": definition["version"],
    }
    source_set = False
    for item_id, item in definition["inputs"].items():
        value = overrides.get(item_id, item["value"])
        if item["type"] == "source" and not source_set:
            value = source_value
            source_set = True
        inputs[item_id] = {"v": value, "f": item["is_fake"], "t": item["type"]}
    if not source_set:
        inputs["in_0"] = {"v": source_value, "f": True, "t": "source"}
    return inputs


def _catalog_row(item: dict, access: str) -> dict:
    extra = item.get("extra") or {}
    return {
        "id": item.get("scriptIdPart") or "",
        "version": str(item.get("version") or "last"),
        "name": item.get("scriptName") or item.get("scriptIdPart") or "",
        "type": extra.get("kind") or "study",
        "access": access,
    }


class TvStudyClient:
    def __init__(
        self,
        *,
        session_id: str | None = None,
        signature: str | None = None,
        http_get=None,
        connect=None,
        connections: int | None = None,
        study_limit: int | None = None,
        history_bars: int = 300,
    ):
        self.session_id = settings.tv_sessionid if session_id is None else session_id.strip()
        self.signature = settings.tv_sessionid_sign if signature is None else signature.strip()
        self._http_get = http_get or _http_get
        self._connect = connect or _connect
        self.connections = settings.tv_chart_connections if connections is None else max(1, int(connections))
        self.study_limit = (
            settings.tv_indicator_on_indicator_limit if study_limit is None else max(1, int(study_limit))
        )
        self.history_bars = history_bars
        # ponytail: one process-wide pool. Per-book pools if two markets scan at once.
        self._slots = threading.BoundedSemaphore(self.connections)
        self._auth_lock = threading.Lock()
        self._auth_token = ""
        self._plan = ""
        self._definitions: dict[tuple[str, str], dict] = {}

    def enabled(self) -> bool:
        return bool(self.session_id and self.signature)

    def session(self) -> dict:
        """``{ok, plan}`` or ``{ok: false, reason}``. No cookie or token."""
        if not self.enabled():
            return {"ok": False, "reason": "disabled"}
        try:
            page = self._http_get(_CHART_PAGE, self._cookies())
        except TvStudyError as exc:
            return {"ok": False, "reason": "expired" if exc.kind == "expired" else exc.kind}
        user = parse_user_page(page)
        if not user["auth_token"]:
            self._auth_token = ""
            return {"ok": False, "reason": "expired"}
        with self._auth_lock:
            self._auth_token = user["auth_token"]
            self._plan = user["plan"]
        return {"ok": True, "plan": user["plan"]}

    def catalog(self) -> dict:
        builtins = [
            _catalog_row(item, "closed_source")
            for item in self._json(f"{_PINE}/list?filter=standard", auth=False)
            if isinstance(item, dict) and item.get("scriptIdPart")
        ]
        scripts: list[dict] = []
        if self.enabled():
            saved = self._json(f"{_PINE}/list?filter=saved", auth=True)
            scripts = [
                _catalog_row(item, "private")
                for item in saved
                if isinstance(item, dict) and item.get("scriptIdPart")
            ]
        return {"builtins": builtins, "scripts": scripts}

    def fetch(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        studies: list[dict],
    ) -> dict:
        if not self.enabled():
            raise TvStudyError("disabled", "TradingView studies are off — set TV_SESSIONID and TV_SESSIONID_SIGN.")
        children = child_study_count(studies)
        if children > self.study_limit:
            raise TvStudyError(
                "error",
                f"This stack has {children} indicator-on-indicator studies; "
                f"the plan cap is {self.study_limit}.",
            )
        if not self._slots.acquire(timeout=30):
            raise TvStudyError("rate_limit", "Chart connection pool is full.")
        try:
            token = self._token()
            definitions = [self._definition(study) for study in studies]
            return self._fetch_socket(symbol, exchange, timeframe, studies, definitions, token)
        finally:
            self._slots.release()

    def _cookies(self) -> dict[str, str]:
        return {"sessionid": self.session_id, "sessionid_sign": self.signature}

    def _token(self) -> str:
        with self._auth_lock:
            token = self._auth_token
        if token:
            return token
        result = self.session()
        if not result.get("ok"):
            raise TvStudyError(
                "expired",
                "TradingView session expired — update TV_SESSIONID.",
            )
        with self._auth_lock:
            return self._auth_token

    def _json(self, url: str, *, auth: bool) -> list:
        try:
            body = self._http_get(url, self._cookies() if auth else None)
        except TvStudyError as exc:
            if not auth:
                raise
            raise TvStudyError("expired", "TradingView session expired — update TV_SESSIONID.") from exc
        data = json.loads(body)
        if not isinstance(data, list):
            raise TvStudyError("expired", "TradingView session expired — update TV_SESSIONID.")
        return data

    def _definition(self, study: dict) -> dict:
        study_id = str(study.get("id") or "")
        version = str(study.get("version") or "last")
        key = (study_id, version)
        cached = self._definitions.get(key)
        if cached is not None:
            return cached
        encoded = quote(study_id, safe="")
        url = f"{_PINE}/translate/{encoded}/{quote(version, safe='')}"
        try:
            body = self._http_get(url, self._cookies())
        except TvStudyError as exc:
            raise TvStudyError("study_not_auth", f"This account cannot open {study_id}.") from exc
        definition = parse_indicator_definition(json.loads(body), study_id, version)
        self._definitions[key] = definition
        return definition

    def _fetch_socket(self, symbol, exchange, timeframe, studies, definitions, token) -> dict:
        chart_id = "cs_bot"
        resolution = "W" if timeframe == "1W" else "D"
        ticker = f"{exchange}:{symbol}"
        symbol_init = json.dumps({"symbol": ticker, "adjustment": "splits"}, separators=(",", ":"))
        commands = [
            encode_packet("set_auth_token", [token]),
            encode_packet("chart_create_session", [chart_id, ""]),
            encode_packet("resolve_symbol", [chart_id, "sds_sym_1", f"={symbol_init}"]),
            encode_packet(
                "create_series",
                [chart_id, "$prices", "s1", "sds_sym_1", resolution, self.history_bars, ""],
            ),
        ]
        ws_ids = [f"st{index + 1}" for index in range(len(studies))]
        for index, (study, definition, ws_id) in enumerate(zip(studies, definitions, ws_ids)):
            source = study.get("source") or {"type": "price", "series": "close"}
            if source.get("type") == "study":
                parent = int(source["study_index"])
                parent_id = ws_ids[parent]
                source_value = f"{parent_id}${plot_index(definitions[parent], str(source.get('plot') or 'plot_0'))}"
                parent_arg = parent_id
            else:
                source_value = str(source.get("series") or "close")
                if source_value not in PRICE_SOURCES:
                    raise TvStudyError("error", f"Unknown price source {source_value}.")
                parent_arg = "$prices"
            inputs = study_inputs(definition, study.get("inputs") or {}, source_value)
            commands.append(encode_packet(
                "create_study",
                [chart_id, ws_id, parent_arg if parent_arg != "$prices" else "st1", parent_arg, definition["type"], inputs],
            ))
        bars: dict[int, dict] = {}
        series: list[dict[int, dict]] = [{} for _ in studies]
        ready: set[str] = set()
        deadline = time.monotonic() + 25
        try:
            with self._connect(_WS, _ws_headers()) as ws:
                _expect_hello(ws, deadline)
                for command in commands:
                    ws.send(command)
                while len(ready) < len(ws_ids) and time.monotonic() < deadline:
                    for frame in decode_frames(_recv(ws, deadline)):
                        if frame["type"] == "heartbeat":
                            ws.send(encode_frame(f"{_HEARTBEAT}{frame['id']}"))
                            continue
                        if frame["type"] != "packet":
                            continue
                        _consume(frame["packet"], ws_ids, definitions, bars, series, ready)
        except TvStudyError:
            raise
        except Exception as exc:
            message = str(exc)
            if "429" in message:
                raise TvStudyError("rate_limit", "TradingView rate limit. Retry later.") from exc
            raise TvStudyError("error", "TradingView chart session failed.") from exc
        if len(ready) < len(ws_ids):
            raise TvStudyError("error", f"TradingView did not finish studies for {symbol}.")
        return {
            "symbol": symbol,
            "exchange": exchange,
            "timeframe": timeframe,
            "bars": [_bar_row(ts, row) for ts, row in sorted(bars.items())],
            "studies": [
                _study_result(index, study, definition, ws_ids[index], series[index])
                for index, (study, definition) in enumerate(zip(studies, definitions))
            ],
        }


def _bar_row(ts: int, row: dict) -> dict:
    return {"time": ts, **row}


def _study_result(index: int, study: dict, definition: dict, ws_id: str, rows: dict[int, dict]) -> dict:
    ordered = [rows[ts] for ts in sorted(rows)]
    frame = pd.DataFrame(ordered).set_index("time") if ordered else pd.DataFrame()
    return {
        "index": index,
        "id": study.get("id") or definition["id"],
        "ws_id": ws_id,
        "plots": ordered,
        "frame": frame,
        "strategy_entries": [],
    }


def _consume(packet, ws_ids, definitions, bars, series, ready) -> None:
    method = packet.get("m")
    params = packet.get("p") or []
    if method in ("timescale_update", "du"):
        data = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
        prices = (data.get("$prices") or {}).get("s") or []
        for bar in prices:
            values = bar.get("v") or []
            if len(values) < 5:
                continue
            bars[int(values[0])] = {
                "open": values[1], "high": values[2], "low": values[3],
                "close": values[4], "volume": values[5] if len(values) > 5 else 0,
            }
        for index, ws_id in enumerate(ws_ids):
            rows = (data.get(ws_id) or {}).get("st") or []
            names = list((definitions[index].get("plots") or {}))
            for row in rows:
                values = row.get("v") or []
                if not values:
                    continue
                point = {"time": int(values[0])}
                for offset, value in enumerate(values[1:]):
                    name = names[offset] if offset < len(names) else f"plot_{offset}"
                    point[name] = value
                    point[f"plot_{offset}"] = value
                series[index][point["time"]] = point
        return
    if method == "study_completed" and len(params) > 1 and params[1] in ws_ids:
        ready.add(params[1])
        return
    if method == "study_error":
        message = _study_error_text(params[3] if len(params) > 3 else params[-1] if params else "")
        kind = "study_not_auth" if "study_not_auth" in message or "not_auth" in message else "error"
        raise TvStudyError(kind, "This account cannot open that study." if kind == "study_not_auth" else message)
    if method in ("symbol_error", "series_error", "critical_error"):
        text = " ".join(str(part) for part in params)
        if "429" in text:
            raise TvStudyError("rate_limit", "TradingView rate limit. Retry later.")
        raise TvStudyError("error", "TradingView rejected the chart session.")


def _study_error_text(error: Any) -> str:
    if isinstance(error, dict):
        return str(error.get("error") or error)
    return str(error)


def _expect_hello(ws, deadline: float) -> None:
    for frame in decode_frames(_recv(ws, deadline)):
        if frame["type"] == "heartbeat":
            ws.send(encode_frame(f"{_HEARTBEAT}{frame['id']}"))
        elif frame["type"] in ("data", "packet"):
            return
    raise TvStudyError("error", "TradingView chart session did not greet.")


def _recv(ws, deadline: float) -> str:
    timeout = max(0.1, deadline - time.monotonic())
    data = ws.recv(timeout)
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return str(data)


def _ws_headers() -> dict[str, str]:
    return {
        "Origin": _ORIGIN,
        "User-Agent": _UA,
    }


def _http_get(url: str, cookies: dict[str, str] | None) -> str:
    headers = {"User-Agent": _UA}
    if cookies:
        headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
    try:
        response = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise TvStudyError("error", "TradingView did not answer.") from exc
    if response.status_code == 429:
        raise TvStudyError("rate_limit", "TradingView rate limit. Retry later.")
    if response.status_code in (401, 403):
        raise TvStudyError("expired", "TradingView session expired — update TV_SESSIONID.")
    if response.status_code >= 400:
        raise TvStudyError("error", f"TradingView returned HTTP {response.status_code}.")
    return response.text


@contextmanager
def _connect(url: str, headers: dict[str, str]) -> Iterator[Any]:
    from websockets.sync.client import connect

    with connect(url, additional_headers=headers, open_timeout=20, close_timeout=5) as ws:
        yield ws
