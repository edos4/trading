"""Authenticated HTTP, run drilldown, chart and complete-export regressions."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core.experiments.catalog import catalog
from core.experiments.contracts import BatchRequest
from core.experiments.service import ExperimentService
from core.experiments.store import ExperimentStore
from core.test_experiments import bars


@pytest.fixture
def app_client(tmp_path):
    service = ExperimentService(ExperimentStore(tmp_path),enumerate_symbols=lambda *_:["SPY"],loader=lambda *_:bars())
    with patch("web.auth.settings") as auth, patch("web.app.settings") as settings, \
            patch("web.experiments.get_service", return_value=service):
        for mocked in (auth,settings):
            mocked.web_ui_password = "test-experiment-password"
            mocked.web_ui_username = "admin"
            mocked.web_ui_users = ""
            mocked.web_ui_secret_key = "experiment-test-only"
            mocked.web_ui_https = False
            mocked.tasks_enabled = False
            mocked.volume_gate_enabled = False
        from web.app import create_app

        app = create_app()
        with TestClient(app) as client:
            client.post("/login",data={"username":"admin","password":"test-experiment-password",
                                       "next":"/experiments"})
            yield client, app, service


def completed(service):
    return service.submit(BatchRequest(start_date="2021-06-01",end_date="2021-11-30",
                                      idempotency_key="http-test"),background=False)


def test_page_catalog_and_auth(app_client):
    client, app, _ = app_client
    response = client.get("/experiments")
    assert response.status_code == 200
    assert 'href="/experiments"' in response.text
    assert 'id="exp-end"' in response.text and 'id="exp-start"' in response.text
    assert 'experiments.js' in response.text
    assert len(client.get("/api/experiments/catalog").json()["sources"]) == 449
    anonymous = TestClient(app)
    for path in ("/experiments","/api/experiments/catalog","/api/experiments/runs",
                 "/api/experiments/runs/nope", "/api/experiments/sources/nope",
                 "/api/experiments/runs/nope/folders/nope/records",
                 "/api/experiments/runs/nope/folders/nope/chart?symbol=SPY",
                 "/api/experiments/runs/nope/folders/nope/export"):
        assert anonymous.get(path).status_code == 401
    assert anonymous.post("/api/experiments/runs",json={}).status_code == 401
    assert anonymous.post("/api/experiments/runs/nope/cancel").status_code == 401


def test_start_validation_and_unknown_ids(app_client):
    client,_,_ = app_client
    for body in ({},[],{"start_date":"2022-01-01","end_date":"2021-01-01","idempotency_key":"x"}):
        assert client.post("/api/experiments/runs",json=body).status_code == 400
    assert client.post("/api/experiments/runs",content="{broken",headers={"content-type":"application/json"}).status_code == 400
    assert client.get("/api/experiments/runs/no-such-run").status_code == 404
    assert client.get("/api/experiments/sources/no-such-source").status_code == 404


def test_folder_drilldown_pagination_export_and_frozen_chart(app_client):
    client,_,service = app_client
    batch = completed(service)
    source = next(s for s in catalog() if s["number"] == 406)
    root = f"/api/experiments/runs/{batch['id']}/folders/{source['id']}"
    detail = client.get(root)
    assert detail.status_code == 200
    assert detail.json()["source"]["plan_sha256"] == source["plan_sha256"]
    assert detail.json()["run"]["summary"]["variants"]
    first = client.get(root+"/records?kind=signals&limit=1").json()
    assert len(first["rows"]) == 1 and first["next"]
    second = client.get(root+f"/records?kind=signals&limit=1&after={first['next']}").json()
    assert second["rows"][0]["cursor"] != first["rows"][0]["cursor"]
    assert client.get(root+"/records?kind=oops").status_code == 400
    assert client.get(root+"/records?limit=10000").status_code == 422
    exported = client.get(root+"/export")
    assert exported.status_code == 200
    lines = [json.loads(line) for line in exported.text.splitlines()]
    assert lines[0]["kind"] == "manifest"
    frozen = lines[0]["provenance"]
    assert frozen["symbols"] == ["SPY"] and frozen["txn_cost_pct"] == .001
    assert frozen["dataset_sha256"] and frozen["engine_sha256"] and frozen["tape_version"] == 2
    assert sum(line["kind"] == "signals" for line in lines) == first["total"]
    with patch("data.history.load_daily_bars",side_effect=AssertionError("Charts must use frozen data")):
        chart = client.get(root+"/chart?symbol=SPY")
    assert chart.status_code == 200,chart.text
    assert chart.json()["replay_id"] == batch["id"]
    assert chart.json()["candles"][-1]["time"] <= "2021-11-30"
    trade=client.get(root+"/records?kind=trades&limit=1").json()["rows"][0]
    saved_chart=client.get(root+f"/chart?symbol=SPY&record={trade['cursor']}")
    assert saved_chart.status_code == 200
    assert any(level["title"] == "entry" for level in saved_chart.json()["levels"])
    assert "Exit fill" in [marker["text"] for marker in saved_chart.json()["markers"]]
    signal = first["rows"][0]
    chart = client.get(root+f"/chart?symbol=SPY&record={signal['cursor']}").json()
    assert [marker["text"] for marker in chart["markers"]] == [signal["status"].replace("-", " ")]
    assert client.get(root+"/chart?symbol=SPY&record=999999999").status_code == 404
    assert client.get(root+"/chart?symbol=UNKNOWN").status_code == 404


def test_blocked_folder_shows_reason_and_not_fake_metrics(app_client):
    client,_,service = app_client
    batch = completed(service)
    source=next(s for s in catalog() if s["number"] == 442)
    detail=client.get(f"/api/experiments/runs/{batch['id']}/folders/{source['id']}").json()
    assert detail["run"]["state"] == "blocked"
    assert detail["run"]["error"] and detail["source"]["gaps"]
    assert not detail["run"]["summary"]


def test_cancel_and_conflict_are_http_errors_not_generic_server_failures(app_client):
    client,_,service = app_client
    manifest=[{**catalog()[0],"availability":"ready"}]
    req=BatchRequest(start_date="2021-06-01",end_date="2021-11-30",idempotency_key="queued")
    batch,_=service.store.create(req.model_dump(mode="json"),manifest)
    assert client.get(f"/api/experiments/runs/{batch}/folders/{manifest[0]['id']}/export").status_code == 409
    response=client.post("/api/experiments/runs",json={**req.model_dump(mode="json"),"idempotency_key":"other"})
    assert response.status_code == 409
    response=client.post(f"/api/experiments/runs/{batch}/cancel")
    assert response.status_code == 200 and response.json()["cancel"] == 1


def test_holdout_evidence_and_export_are_separate(app_client):
    client, _, service = app_client
    batch = service.submit(BatchRequest(start_date="2020-06-01", end_date="2021-11-30",
        holdout_start="2021-06-01", idempotency_key="holdout-test"), background=False)
    src = next(s["id"] for s in catalog() if s["number"] == 120)
    path = f"/api/experiments/runs/{batch['id']}/folders/{src}"
    assert batch["progress"]["evaluation"]["holdout_start"] == "2021-06-01"
    summary = client.get(path).json()["run"]["summary"]
    assert summary["trade_count"] and summary["holdout"]["trade_count"]
    rows = client.get(path+"/records?kind=holdout-trades&chronological=true").json()["rows"]
    assert rows and all(t["signal_date"] >= "2021-06-01" for t in rows)
    exported = [json.loads(line) for line in client.get(path+"/export").text.splitlines()]
    assert sum(row["kind"]=="holdout-trades" for row in exported) == len(rows)
