"""ASGI middleware: request context, security headers, CSRF, body limits.

CSRF uses a *signed double-submit* token bound to the session cookie:
    cookie  snx_csrf = <token>.<HMAC(SECRET_KEY, token + session_cookie)>
    field   csrf_token / header X-CSRF-Token = <token>
An attacker on another origin can neither read the token nor forge the signature,
and the binding means a token minted for one session cannot be replayed on another.
No database access is needed in the middleware, so it never blocks the event loop.
"""
from __future__ import annotations

import re

import hashlib
import hmac
import secrets
import time
from urllib.parse import parse_qs

from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import audit
from app.core.config import get_settings
from app.db.base import new_uuid

_cfg = get_settings()
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_COOKIE = "snx_csrf"
MAX_BODY = 1_000_000
MULTIPART_OVERHEAD = 256 * 1024   # form fields and boundaries around an uploaded picture
_MULTIPART_CSRF = re.compile(rb'name="csrf_token"\r\n\r\n([^\r\n]{1,200})\r\n')
EXEMPT_PREFIXES = ("/static/", "/healthz")
# Credentials are in the body and no cookie is used, so there is nothing for CSRF to protect.
# Brute force is limited by ratelimit + account lockout inside auth.authenticate().
EXEMPT_EXACT = ("/api/v1/auth/token",)


def sign_csrf(token: str, session_cookie: str) -> str:
    mac = hmac.new(_cfg.SECRET_KEY.encode(), f"{token}{session_cookie}".encode(), hashlib.sha256)
    return mac.hexdigest()


def make_csrf_cookie(session_cookie: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(24)
    return token, f"{token}.{sign_csrf(token, session_cookie)}"


def csrf_cookie_valid(cookie_value: str | None, presented: str | None, session_cookie: str) -> bool:
    if not cookie_value or not presented or "." not in cookie_value:
        return False
    token, signature = cookie_value.rsplit(".", 1)
    if len(token) > 128 or len(presented) > 128:
        return False
    if not hmac.compare_digest(token, presented):
        return False
    return hmac.compare_digest(signature, sign_csrf(token, session_cookie))


def cookie_header(name: str, value: str, *, max_age: int, http_only: bool, secure: bool,
                  same_site: str, path: str = "/") -> str:
    parts = [f"{name}={value}", f"Path={path}", f"Max-Age={max_age}", "SameSite=" + same_site.capitalize()]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    if same_site == "none" and secure:
        parts.append("Partitioned")  # CHIPS: works inside the embedded preview iframe
    return "; ".join(parts)


import ipaddress


def _parse_nets(values):
    nets = []
    for value in values:
        try:
            nets.append(ipaddress.ip_network(str(value).strip(), strict=False))
        except ValueError:
            continue
    return nets


_TRUSTED_NETS = _parse_nets(_cfg.TRUSTED_PROXIES)


def _is_trusted(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED_NETS)


def resolve_client_ip(peer: str, forwarded: str) -> str:
    """X-Forwarded-For is only believed when the direct peer is a configured proxy;
    then the right-most hop that is not itself a trusted proxy is the client."""
    if not forwarded or not _is_trusted(peer):
        return peer
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer
        if not _is_trusted(hop):
            return hop
    return peer


class RequestContextMiddleware:
    """Assigns a request id and populates the audit context vars."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        request_id = new_uuid()[:16]
        peer = (scope.get("client") or ("unknown", 0))[0]
        ip = resolve_client_ip(peer, headers.get(b"x-forwarded-for", b"").decode("latin-1"))
        audit.set_context(request_id, ip, headers.get(b"user-agent", b"").decode()[:255])
        scope.setdefault("state", {})["request_id"] = request_id
        scope["state"]["client_ip"] = ip

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers_list = message.setdefault("headers", [])
                headers_list.append((b"x-request-id", request_id.encode()))
            await send(message)

        await self.app(scope, receive, send_wrapper)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        csp = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'self'; "
            f"frame-ancestors {_cfg.FRAME_ANCESTORS}"
        )
        is_static = scope["path"].startswith("/static/")
        # Build output is content-hashed (tools/build_static.mjs): a name never changes meaning, so cache for good.
        is_hashed = scope["path"].startswith("/static/dist/")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.extend([
                    (b"content-security-policy", csp.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", b"geolocation=(), microphone=(), camera=(self), payment=()"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"x-frame-options", b"SAMEORIGIN" if _cfg.ENV == "dev" else b"DENY"),
                ])
                if _cfg.ENV == "prod":
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                # A route that chose its own Cache-Control keeps it (this used to append "no-store" next to it, which
                # won, so /manifest.webmanifest and /sw.js could never be cached or revalidated). Every other
                # response still gets the same defaults as before.
                if not any(name.lower() == b"cache-control" for name, _ in headers):
                    if is_hashed:
                        headers.append((b"cache-control", b"public, max-age=31536000, immutable"))
                    elif is_static:
                        headers.append((b"cache-control", b"public, max-age=3600"))
                    else:
                        headers.append((b"cache-control", b"no-store"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


class CsrfMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        if method in SAFE_METHODS or path.startswith(EXEMPT_PREFIXES) or path in EXEMPT_EXACT:
            return await self.app(scope, receive, send)

        headers = {k.decode().lower(): v.decode() for k, v in (scope.get("headers") or [])}
        # Bearer-authenticated API calls are not ambient-credential requests.
        if headers.get("authorization", "").startswith("Bearer ") and path.startswith("/api/"):
            return await self.app(scope, receive, send)

        cookies = _parse_cookies(headers.get("cookie", ""))
        presented = headers.get("x-csrf-token")

        buffered: list[Message] = []
        body = b""
        content_type = headers.get("content-type", "")
        is_multipart = content_type.startswith("multipart/form-data")
        # Only a multipart form (the result-picture upload) may be bigger than a normal form.
        limit = (_cfg.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD) if is_multipart else MAX_BODY
        if presented is None:
            while True:
                message = await receive()
                buffered.append(message)
                if message["type"] != "http.request":
                    break
                body += message.get("body", b"")
                if len(body) > limit:
                    response = PlainTextResponse("Request body too large", status_code=413)
                    return await response(scope, receive, send)
                if not message.get("more_body", False):
                    break
            if is_multipart:
                match = _MULTIPART_CSRF.search(body)
                presented = match.group(1).decode("ascii", "ignore") if match else None
            elif content_type.startswith("application/x-www-form-urlencoded"):
                fields = parse_qs(body.decode("utf-8", "replace"))
                presented = (fields.get("csrf_token") or [None])[0]
            elif content_type.startswith("application/json"):
                import json
                try:
                    presented = json.loads(body or b"{}").get("csrf_token")
                except (ValueError, AttributeError):
                    presented = None

        if not csrf_cookie_valid(cookies.get(CSRF_COOKIE), presented, cookies.get(_cfg.SESSION_COOKIE, "")):
            payload = {"detail": "CSRF validation failed. Reload the page and try again."}
            response = JSONResponse(payload, status_code=403)
            return await response(scope, receive, send)

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay if buffered else receive, send)


def _parse_cookies(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" in chunk:
            name, value = chunk.split("=", 1)
            out[name.strip()] = value.strip()
    return out


class IpBanMiddleware:
    """403 for banned addresses, before CSRF, auth or any route. Needs RequestContextMiddleware
    outside it (it sets ``state.client_ip``). /healthz is exempt so a load balancer is never banned
    by accident; the snapshot refresh runs in a worker thread so the event loop is never blocked."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.concurrency import run_in_threadpool
        from app.services import ipbans
        if ipbans.snapshot_stale():
            await run_in_threadpool(ipbans.refresh)
        ip = scope.get("state", {}).get("client_ip")
        if ip and not scope["path"].startswith("/healthz") and ipbans.is_banned(ip):
            response = PlainTextResponse("Access from your address has been blocked.", status_code=403)
            return await response(scope, receive, send)
        await self.app(scope, receive, send)
