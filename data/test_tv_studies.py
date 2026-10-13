"""TradingView study client: session check and a two-study fetch. No live cookies."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from data.tv_studies import (
    TvStudyClient,
    TvStudyError,
    decode_frames,
    encode_frame,
)


def _page(token="secret-token", plan="pro_plus"):
    return f'"auth_token":"{token}","pro_plan":"{plan}"'


def _definition(study_id):
    return json.dumps({
        "success": True,
        "result": {
            "ilTemplate": "IL",
            "metaInfo": {
                "scriptIdPart": study_id,
                "description": study_id,
                "inputs": [
                    {"id": "in_0", "name": "Length", "type": "integer", "defval": 14, "isFake": False},
                    {"id": "in_1", "name": "Source", "type": "source", "defval": "close", "isFake": True},
                ],
                "styles": {"plot_0": {"title": "Plot"}},
                "pine": {"version": "1"},
            },
        },
    })


class ScriptedSocket:
    def __init__(self):
        self.sent = []
        self.inbox = [encode_frame({"session_id": "x", "protocol": "json"})]

    def send(self, data):
        self.sent.append(data)
        for frame in decode_frames(data):
            packet = frame.get("packet") or {}
            if packet.get("m") == "create_study" and packet["p"][1] == "st2":
                self.inbox.append(encode_frame({
                    "m": "timescale_update",
                    "p": ["cs_bot", {
                        "$prices": {"s": [
                            {"i": 0, "v": [1_700_000_000, 10, 11, 9, 10.5, 100]},
                            {"i": 1, "v": [1_700_086_400, 10.5, 12, 10, 11.5, 120]},
                        ]},
                        "st1": {"st": [
                            {"v": [1_700_000_000, 40]},
                            {"v": [1_700_086_400, 60]},
                        ]},
                        "st2": {"st": [
                            {"v": [1_700_000_000, 30]},
                            {"v": [1_700_086_400, 55]},
                        ]},
                    }],
                }))
                self.inbox.append(encode_frame({"m": "study_completed", "p": ["cs_bot", "st1"]}))
                self.inbox.append(encode_frame({"m": "study_completed", "p": ["cs_bot", "st2"]}))

    def recv(self, timeout=None):
        if not self.inbox:
            raise TimeoutError("scripted socket is empty")
        return self.inbox.pop(0)


def test_session_disabled_does_not_call_transport():
    def http_get(url, cookies):
        raise AssertionError(url)

    client = TvStudyClient(session_id="", signature="", http_get=http_get)
    assert client.session() == {"ok": False, "reason": "disabled"}


def test_session_http_error_is_expired_and_hides_the_token():
    def http_get(url, cookies):
        raise TvStudyError("expired", "TradingView session expired — update TV_SESSIONID.")

    client = TvStudyClient(session_id="sid", signature="sig", http_get=http_get)
    result = client.session()
    assert result == {"ok": False, "reason": "expired"}
    assert "sid" not in json.dumps(result)


def test_session_without_auth_token_is_expired():
    client = TvStudyClient(
        session_id="sid", signature="sig", http_get=lambda url, cookies: "<html>login</html>",
    )
    assert client.session() == {"ok": False, "reason": "expired"}


def test_session_ok_returns_plan_only():
    client = TvStudyClient(
        session_id="sid", signature="sig", http_get=lambda url, cookies: _page(),
    )
    result = client.session()
    assert result == {"ok": True, "plan": "pro_plus"}
    assert "secret-token" not in json.dumps(result)


def test_two_study_fetch_parses_periods_and_parent_plot():
    socket = ScriptedSocket()

    def http_get(url, cookies):
        if "translate" in url:
            study_id = "STD;SMA" if "SMA" in url else "STD;RSI"
            return _definition(study_id)
        return _page()

    @contextmanager
    def connect(url, headers):
        assert "prodata.tradingview.com" in url
        yield socket

    client = TvStudyClient(
        session_id="sid", signature="sig", http_get=http_get, connect=connect,
        connections=2, study_limit=9,
    )
    result = client.fetch("AAPL", "NASDAQ", "1d", [
        {"id": "STD;RSI", "version": "last", "inputs": {}, "source": {"type": "price", "series": "close"}},
        {"id": "STD;SMA", "version": "last", "inputs": {}, "source": {"type": "study", "study_index": 0, "plot": "plot_0"}},
    ])
    child = result["studies"][1]["frame"]
    assert list(child.index) == [1_700_000_000, 1_700_086_400]
    assert child.loc[1_700_086_400, "plot_0"] == 55
    assert result["studies"][0]["frame"].loc[1_700_086_400, "plot_0"] == 60
    packets = []
    for raw in socket.sent:
        packets.extend(
            frame["packet"] for frame in decode_frames(raw) if frame["type"] == "packet"
        )
    created = [packet for packet in packets if packet["m"] == "create_study"]
    assert created[0]["p"][3] == "$prices"
    assert created[0]["p"][5]["in_1"]["v"] == "close"
    assert created[1]["p"][3] == "st1"
    assert created[1]["p"][5]["in_1"]["v"] == "st1$0"


def test_child_study_cap_rejects_before_the_socket():
    def connect(url, headers):
        raise AssertionError("socket opened")

    client = TvStudyClient(
        session_id="sid", signature="sig", connect=connect, study_limit=1,
        http_get=lambda url, cookies: _page(),
    )
    studies = [
        {"id": "STD;RSI", "source": {"type": "price", "series": "close"}},
        {"id": "STD;SMA", "source": {"type": "study", "study_index": 0, "plot": "plot_0"}},
        {"id": "STD;EMA", "source": {"type": "study", "study_index": 1, "plot": "plot_0"}},
    ]
    with pytest.raises(TvStudyError, match="plan cap is 1"):
        client.fetch("AAPL", "NASDAQ", "1d", studies)


def test_pool_size_follows_the_connection_cap():
    client = TvStudyClient(session_id="", signature="", connections=4)
    assert client.connections == 4
    assert client._slots._initial_value == 4
