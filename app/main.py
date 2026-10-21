from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.api import approvals, runs, workers, workflows
from app.config import get_settings
from app.core.errors import AppError
from app.core.rate_limit import RateLimiter
from app.db import SessionLocal, init_db
from app.observability.logging import configure_logging, request_id_var
from app.observability.metrics import render_metrics
from app.observability.tracing import setup_tracing
from app.services.runs import RunError
from app.services.workflows import WorkflowSpecError, seed_builtin_workflows

settings = get_settings()
templates = Jinja2Templates(directory="app/templates")
limiter = RateLimiter(settings)

SKIP_RATE_LIMIT = {"/healthz", "/metrics", "/ui", "/docs", "/openapi.json", "/redoc", "/"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    setup_tracing()
    init_db()
    with SessionLocal() as session:
        seed_builtin_workflows(session)
    yield


app = FastAPI(
    title="DurableFlow",
    version="0.1.0",
    description="轻量级持久化工作流引擎：PostgreSQL 状态、Redis 租约、幂等 Outbox、可恢复 Worker。",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = rid
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in SKIP_RATE_LIMIT:
        return await call_next(request)
    client = request.client.host if request.client else "unknown"
    if not limiter.allow(client):
        return JSONResponse(
            status_code=429,
            content={
                "detail": "rate limit exceeded",
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "rate limit exceeded",
                    "details": None,
                    "request_id": getattr(request.state, "request_id", None),
                },
            },
            headers={"Retry-After": "60", "X-RateLimit-Limit": str(limiter.limit)},
        )
    response = await call_next(request)
    response.headers["X-RateLimit-Limit"] = str(limiter.limit)
    return response


_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
}


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    details: object = None,
    headers: dict | None = None,
) -> JSONResponse:
    """统一错误结构；保留 detail 字段以兼容旧客户端。"""
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": message,
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": getattr(request.state, "request_id", None),
            },
        },
        headers=headers,
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return _error_response(
        request,
        exc.status_code,
        exc.code,
        exc.message,
        details=exc.details,
    )


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "请求失败"
    details = None if isinstance(exc.detail, str) else exc.detail
    return _error_response(
        request,
        exc.status_code,
        _STATUS_CODES.get(exc.status_code, "HTTP_ERROR"),
        message,
        details=details,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return _error_response(
        request,
        422,
        "VALIDATION_ERROR",
        "请求参数校验失败",
        details=jsonable_encoder(exc.errors()),
    )


@app.exception_handler(WorkflowSpecError)
async def workflow_spec_error_handler(request: Request, exc: WorkflowSpecError):
    return _error_response(request, 400, "WORKFLOW_SPEC_INVALID", str(exc))


@app.exception_handler(RunError)
async def run_error_handler(request: Request, exc: RunError):
    return _error_response(request, 400, "RUN_ERROR", str(exc))


# 新版统一走 /api/v1；/api 保留给现有 UI 和旧脚本，不进入 OpenAPI 文档。
for prefix, in_schema in (("/api/v1", True), ("/api", False)):
    app.include_router(workflows.router, prefix=prefix, include_in_schema=in_schema)
    app.include_router(runs.router, prefix=prefix, include_in_schema=in_schema)
    app.include_router(runs.start_router, prefix=prefix, include_in_schema=in_schema)
    app.include_router(approvals.router, prefix=prefix, include_in_schema=in_schema)
    app.include_router(workers.router, prefix=prefix, include_in_schema=in_schema)


@app.get("/")
def root():
    return RedirectResponse(url="/ui")


@app.get("/healthz")
def healthz():
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "down", "database": str(exc)})

    redis_status = "disabled"
    if settings.redis_enabled:
        try:
            import redis

            redis.Redis.from_url(settings.redis_url, socket_timeout=1).ping()
            redis_status = "ok"
        except Exception:  # noqa: BLE001
            redis_status = "degraded"
    return {"status": "ok", "database": "ok", "redis": redis_status, "rate_limit_backend": limiter.backend}


@app.get("/metrics")
def metrics():
    content, content_type = render_metrics()
    return Response(content=content, media_type=content_type)


@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request):
    return templates.TemplateResponse(request, "index.html", {"request": request})
