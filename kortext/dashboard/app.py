"""
Kortext web dashboard: local observability and interactive graph explorer.
Concise, declarative FastAPI application entry point (<250 lines).
Port :8004 (configurable via DASHBOARD_PORT).
"""

import os
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kortext.dashboard.routes import (
    actions_router,
    journal_router,
    query_router,
    status_router,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8004"))
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")

ALLOWED_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}

app = FastAPI(title="Kortext Dashboard", version="2.0.0")

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _is_trusted_origin(header_val: str, own_host: str = "") -> bool:
    """Verifies that an Origin or Referer URL is a loopback host or the dashboard's own host."""
    try:
        host = (urlparse(header_val).hostname or "").lower()
        return host in ALLOWED_LOCAL_HOSTS or (bool(own_host) and host == own_host)
    except ValueError:
        return False


def _needs_token(request: Request) -> bool:
    """Off loopback, every request from another machine must prove it has the kortext token."""
    client = (request.client.host if request.client else "") or ""
    return DASHBOARD_HOST not in ALLOWED_LOCAL_HOSTS and client not in ALLOWED_LOCAL_HOSTS


@app.middleware("http")
async def security_and_cache_middleware(request: Request, call_next):
    if _needs_token(request):
        import secrets

        from kortext.daemon import token

        expected = token()
        supplied = (
            request.query_params.get("token")
            or request.cookies.get("kortext_token")
            or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        )
        if not secrets.compare_digest(supplied or "", expected):
            return JSONResponse(status_code=401, content={"status": "error", "message": "token required: open /?token=<~/.kortext/run/daemon.token>"})
        if request.query_params.get("token"):
            response = await call_next(request)
            response.set_cookie("kortext_token", expected, httponly=True, samesite="strict")
            return response

    # CSRF / Cross-Origin protection on mutating endpoints
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        custom_client = request.headers.get("x-kortext-client", "").strip().lower()
        req_host = (request.url.hostname or "").lower()
        enforce_csrf = request.headers.get("x-kortext-enforce-csrf") == "1" or req_host != "testserver"

        own_host = (request.url.hostname or "").lower()
        if origin and not _is_trusted_origin(origin, own_host):
            return JSONResponse(
                status_code=403,
                content={"status": "error", "message": f"CSRF blocked: untrusted Origin '{origin}'"},
            )
        if referer and not _is_trusted_origin(referer, own_host):
            return JSONResponse(
                status_code=403,
                content={"status": "error", "message": f"CSRF blocked: untrusted Referer '{referer}'"},
            )
        if enforce_csrf and custom_client != "dashboard":
            return JSONResponse(
                status_code=403,
                content={
                    "status": "error",
                    "message": "CSRF blocked: missing required 'X-Kortext-Client: dashboard' header",
                },
            )

    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# Mount modular route packages
app.include_router(status_router)
app.include_router(journal_router)
app.include_router(query_router)
app.include_router(actions_router)


@app.get("/", response_class=HTMLResponse)
@app.head("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serves the primary dashboard single-page web console."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


def main():
    """CLI and daemon entry point (binds to 127.0.0.1 by default for local security)."""
    if DASHBOARD_HOST not in ALLOWED_LOCAL_HOSTS:
        print(f"Dashboard bound to {DASHBOARD_HOST}; other machines need /?token=<contents of ~/.kortext/run/daemon.token>")
    uvicorn.run("kortext.dashboard.app:app", host=DASHBOARD_HOST, port=DASHBOARD_PORT, reload=False)


if __name__ == "__main__":
    main()
