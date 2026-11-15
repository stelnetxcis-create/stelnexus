"""StelNexus application entry point.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import get_settings
from app.core.errors import AuthError, StelError
from app.core.middleware import (
    CsrfMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.db.session import engine
from app.web.deps import flash_redirect, render, templates

log = logging.getLogger("stelnexus")
_cfg = get_settings()


def create_app() -> FastAPI:
    app = FastAPI(
        title="StelNexus",
        version="1.0.0",
        description="Betting operations console — HTML console + JSON API for mobile.",
        docs_url="/api/docs" if _cfg.ENV == "dev" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if _cfg.ENV == "dev" else None,
    )

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Order matters: context -> security headers -> CSRF -> routes.
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    # Outermost: reject unknown Host headers before anything else runs (prod requires explicit hosts).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_cfg.TRUSTED_HOSTS or ["*"])
    try:
        from app.db.migrate import upgrade
        upgrade(engine)
    except Exception:  # an older database must still boot; the helper logs loudly
        log.exception("schema upgrade failed")
    try:
        from app.db.hardening import install_audit_guards
        install_audit_guards(engine)
    except Exception:  # never block startup; the helper logs loudly
        log.exception("audit-log guards not installed")

    from app.web import (
        routes_admin, routes_agents, routes_auth, routes_bets, routes_dashboard,
        routes_extra, routes_fights, routes_money, routes_players, routes_referrals,
    )
    for module in (routes_auth, routes_dashboard, routes_agents, routes_players,
                   routes_fights, routes_bets, routes_money, routes_admin, routes_referrals,
                   routes_extra):
        app.include_router(module.router)

    from app.api.v1 import routes as api_routes
    app.include_router(api_routes.router, prefix="/api/v1")

    register_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        from sqlalchemy import text
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "brand": "StelNexus"}

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        from fastapi.responses import FileResponse
        return FileResponse(static_dir / "manifest.webmanifest",
                            media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        from fastapi.responses import FileResponse
        return FileResponse(static_dir / "sw.js", media_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})

    return app


def _wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _safe_back(request: Request) -> str:
    referer = request.headers.get("referer") or ""
    if referer:
        parsed = urlparse(referer)
        if not parsed.netloc or parsed.netloc == request.url.netloc:
            path = parsed.path or "/"
            return path + (f"?{parsed.query}" if parsed.query else "")
    return "/dashboard"


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(StelError)
    async def stel_error(request: Request, exc: StelError):
        if _wants_json(request):
            return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=exc.status_code)
        if isinstance(exc, AuthError):
            return flash_redirect("/login", exc.message, "warning")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            return flash_redirect(_safe_back(request), exc.message, "danger")
        return render_error(request, exc.status_code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if _wants_json(request):
            return JSONResponse({"error": "http_error", "detail": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 404:
            return render_error(request, 404, "That page does not exist.")
        return render_error(request, exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        message = "Some fields were invalid. Please check and try again."
        if _wants_json(request):
            return JSONResponse({"error": "validation_error", "detail": message,
                                 "fields": [e["loc"][-1] for e in exc.errors()][:8]}, status_code=422)
        return flash_redirect(_safe_back(request), message, "danger")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # Never leak stack traces, SQL or credentials to the client.
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        detail = "Something went wrong. The incident has been logged."
        if _wants_json(request):
            return JSONResponse({"error": "server_error", "detail": detail}, status_code=500)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            return flash_redirect(_safe_back(request), detail, "danger")
        return render_error(request, 500, detail)


def render_error(request: Request, status_code: int, message: str):
    from app.db.session import SessionLocal
    from app.core.settings_registry import SettingsStore
    from app.services import auth as auth_service

    db = SessionLocal()
    try:
        store = SettingsStore(db)
        resolved = auth_service.resolve_session(db, request.cookies.get(_cfg.SESSION_COOKIE), store)
        user = resolved[1] if resolved else None
        context = {
            "request": request, "ctx": None, "user": user, "perms": set(),
            "settings": store.all(), "brand": store.get("brand.name"),
            "tagline": store.get("brand.tagline"), "csrf_token": "", "flashes": [],
            "notifications": [], "active": "", "status_code": status_code, "message": message,
        }
        return templates.TemplateResponse(request, f"errors/{status_code}.html"
                                          if status_code in (404, 500) else "errors/500.html",
                                          context, status_code=status_code)
    finally:
        db.close()


app = create_app()
