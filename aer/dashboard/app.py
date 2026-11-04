"""Small authenticated HTTP surface; every data operation is read-only."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque
from pathlib import Path
from threading import Lock

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from aer import AER
from aer.dashboard import service

COOKIE = "aer_dashboard_session"
SESSION_SECONDS = 3600
ASSETS = Path(__file__).parent / "static"


def create_app(aer: AER, password: str, secret: str) -> Starlette:
    """Create an app for one trusted data store, never selected by request input."""
    if len(password) < 16 or len(secret) < 32:
        raise ValueError("Dashboard password must be >=16 and session secret >=32 characters")
    expected = hashlib.sha256(password.encode()).digest()
    failures: deque[float] = deque(maxlen=20)
    lock = Lock()

    def sign(value: str) -> str:
        return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()

    def authenticated(request: Request) -> bool:
        value, _, signature = request.cookies.get(COOKIE, "").partition(".")
        try:
            age = time.time() - int(value)
        except ValueError:
            return False
        return 0 <= age <= SESSION_SECONDS and hmac.compare_digest(signature, sign(value))

    async def login(request: Request) -> Response:
        # JSON plus same-origin browser policy prevents cross-origin form login.
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"error": "json_required"}, status_code=415)
        with lock:
            now = time.monotonic()
            while failures and now - failures[0] > 60:
                failures.popleft()
            if len(failures) >= 10:
                return JSONResponse({"error": "too_many_attempts"}, status_code=429)
            failures.append(now)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return JSONResponse({"error": "body_too_large"}, status_code=413)
        try:
            import json

            payload = json.loads(body)
            supplied = payload.get("password") if isinstance(payload, dict) else None
        except (ValueError, RecursionError):
            supplied = None
        if not isinstance(supplied, str) or not hmac.compare_digest(
            hashlib.sha256(supplied.encode()).digest(), expected
        ):
            return JSONResponse({"error": "invalid_credentials"}, status_code=401)
        value = str(int(time.time()))
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            COOKIE,
            f"{value}.{sign(value)}",
            httponly=True,
            samesite="strict",
            max_age=SESSION_SECONDS,
            secure=request.url.scheme == "https",
        )
        return response

    async def logout(request: Request) -> Response:
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE)
        return response

    def data(request: Request) -> Response:
        if not authenticated(request):
            return JSONResponse({"error": "authentication_required"}, status_code=401)
        try:
            offset = int(request.query_params.get("offset", "0"))
            limit = int(request.query_params.get("limit", "25"))
            if not 0 <= offset <= 100000 or not 1 <= limit <= 100:
                raise ValueError("pagination out of bounds")
            kind = request.path_params["kind"]
            identifier = request.path_params.get("identifier")
            status = request.query_params.get("status") or None
            result: object
            if kind == "overview" and identifier is None:
                result = service.overview(aer)
            elif kind == "runs":
                result = (
                    service.run_detail(aer, identifier, offset, limit)
                    if identifier
                    else service.runs(aer, offset, limit, status)
                )
            elif kind == "experiences":
                result = (
                    service.experience_detail(aer, identifier)
                    if identifier
                    else service.experiences(aer, offset, limit, status)
                )
            else:
                result = None
            return JSONResponse(
                result if result is not None else {"error": "not_found"},
                status_code=200 if result is not None else 404,
            )
        except ValueError:
            return JSONResponse({"error": "invalid_query"}, status_code=400)

    def asset(request: Request) -> Response:
        name = request.path_params.get("name", "index.html")
        if name not in {"index.html", "app.js", "style.css"}:
            return Response(status_code=404)
        return FileResponse(ASSETS / name)

    app = Starlette(
        routes=[
            Route("/healthz", lambda request: JSONResponse({"status": "ok"})),
            Route("/", asset),
            Route("/assets/{name}", asset),
            Route("/login", login, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/api/{kind}", data),
            Route("/api/{kind}/{identifier}", data),
        ]
    )

    async def headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=headers)
    return app
