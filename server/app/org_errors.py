import re
import uuid

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_ERROR_CODES = {
    400: "VALIDATION_FAILED",
    401: "AUTH_REQUIRED",
    403: "RESOURCE_FORBIDDEN",
    404: "RESOURCE_NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "STATE_CONFLICT",
    422: "VALIDATION_FAILED",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    503: "UPSTREAM_UNAVAILABLE",
}


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value or "req_" + uuid.uuid4().hex


def install_org_error_handlers(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request.state.request_id = supplied if _REQUEST_ID.fullmatch(supplied) else "req_" + uuid.uuid4().hex
        try:
            response = await call_next(request)
        except Exception:
            if not request.url.path.startswith("/api/v1/"):
                raise
            response = JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": "Internal server error",
                        "retryable": True,
                    },
                    "request_id": request.state.request_id,
                },
            )
        response.headers["X-Request-ID"] = request.state.request_id
        if request.url.path.startswith("/api/v1/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(HTTPException)
    async def org_http_error(request: Request, exc: HTTPException):
        if not request.url.path.startswith("/api/v1/"):
            return await http_exception_handler(request, exc)
        status = exc.status_code
        message = exc.detail if isinstance(exc.detail, str) else "Request could not be completed"
        error = {
            "code": _ERROR_CODES.get(status, "REQUEST_FAILED"),
            "message": message,
            "retryable": status in (429, 500, 502, 503, 504),
        }
        if not isinstance(exc.detail, str):
            error["details"] = exc.detail
        return JSONResponse(
            status_code=status,
            content={"error": error, "request_id": _request_id(request)},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def org_validation_error(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith("/api/v1/"):
            return await request_validation_exception_handler(request, exc)
        details = [
            {"loc": list(error["loc"]), "message": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_FAILED",
                    "message": "Request validation failed",
                    "retryable": False,
                    "details": details,
                },
                "request_id": _request_id(request),
            },
        )

    @app.exception_handler(Exception)
    async def org_internal_error(request: Request, exc: Exception):
        if not request.url.path.startswith("/api/v1/"):
            return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Internal server error",
                    "retryable": True,
                },
                "request_id": _request_id(request),
            },
        )
