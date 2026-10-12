"""Deploy gateway — authenticated HTTPS webhook that deploys this repo.

Run (behind run_deploy.sh's Cloudflare Tunnel):

    python -m web.deploy_gateway

Flow: another computer pushes a branch to GitHub, then calls

    POST /deploy   {"ref": "<branch>", "commit": "<sha>", "force": false}
    Authorization: Bearer <DEPLOY_GATEWAY_TOKEN>

The gateway fetches that ref, checks it out, syncs deps and restarts the web
service. Because this endpoint runs code, it binds loopback-only, requires a
token, and only ever runs the fixed steps in web.deploy_runner.
"""

from __future__ import annotations

import secrets
import threading

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config import settings
from utils.logger import log
from web.deploy_runner import DeployError, deploy, repo_status


class DeployRequest(BaseModel):
    ref: str
    commit: str | None = None
    force: bool = False


def require_token(request: Request) -> None:
    configured = (settings.deploy_gateway_token or "").strip()
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="DEPLOY_GATEWAY_TOKEN is not set — the gateway is disabled.",
        )
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        supplied = header.split(" ", 1)[1].strip()
    else:
        supplied = (request.headers.get("x-deploy-token") or "").strip()
    if not supplied or not secrets.compare_digest(supplied, configured):
        raise HTTPException(status_code=401, detail="invalid deploy token")


def _caller(request: Request) -> str:
    return (
        request.headers.get("cf-connecting-ip")
        or (request.client.host if request.client else "?")
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Trading Bot Deploy Gateway", docs_url=None, redoc_url=None)
    app.state.deploy_lock = threading.Lock()

    @app.get("/health")
    async def health():
        repo = settings.deploy_repo_path
        try:
            branch, sha, dirty = repo_status(repo)
            return {"ok": True, "repo": str(repo), "branch": branch, "commit": sha, "dirty": dirty}
        except Exception as exc:  # noqa: BLE001 — health must not raise
            return {"ok": False, "repo": str(repo), "error": str(exc)}

    @app.post("/deploy", dependencies=[Depends(require_token)])
    async def deploy_endpoint(payload: DeployRequest, request: Request):
        lock: threading.Lock = app.state.deploy_lock
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="a deploy is already running")
        try:
            caller = _caller(request)
            log.info(f"Deploy | request from {caller} ref={payload.ref!r} force={payload.force}")
            result = await run_in_threadpool(
                deploy,
                settings.deploy_repo_path,
                payload.ref,
                payload.commit,
                force=payload.force,
                allow_dirty=settings.deploy_allow_dirty,
                restart_units=settings.deploy_units,
                allowed_refs=settings.deploy_ref_allowlist,
            )
            return result.as_dict()
        except DeployError as exc:
            log.warning(f"Deploy | refused ({exc.status}): {exc}")
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        finally:
            lock.release()

    return app


def run() -> None:
    if not (settings.deploy_gateway_token or "").strip():
        raise RuntimeError(
            "DEPLOY_GATEWAY_TOKEN is empty. Set a strong token in .env before "
            "starting the deploy gateway (refusing to expose a deploy endpoint)."
        )
    import uvicorn

    host = settings.deploy_gateway_host
    port = int(settings.deploy_gateway_port)
    log.info(f"Deploy gateway | starting on http://{host}:{port} (repo={settings.deploy_repo_path})")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
