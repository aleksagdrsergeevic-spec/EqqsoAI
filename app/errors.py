"""Errors in the shape OpenAI clients expect.

Agent SDKs parse {"error": {"message", "type", "code"}} and show the raw body
when it looks different, so every failure path in the gateway raises
GatewayError instead of returning FastAPI's default {"detail": ...}.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("gateway")


class GatewayError(Exception):
    def __init__(self, status_code, message, *, type="invalid_request_error", code=None, param=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type
        self.code = code
        self.param = param

    def to_dict(self):
        return {
            "error": {
                "message": self.message,
                "type": self.type,
                "param": self.param,
                "code": self.code,
            }
        }


def _type_for(status_code: int) -> str:
    if status_code == 400:
        return "invalid_request_error"
    if status_code == 401:
        return "invalid_request_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "invalid_request_error"
    if status_code == 429:
        return "rate_limit_error"
    return "api_error"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway_error(request: Request, exc: GatewayError):
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        error = GatewayError(exc.status_code, message, type=_type_for(exc.status_code))
        return JSONResponse(status_code=exc.status_code, content=error.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        error = GatewayError(400, f"Invalid request body: {exc.errors()}", code="invalid_body")
        return JSONResponse(status_code=400, content=error.to_dict())

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        error = GatewayError(500, f"Internal gateway error: {exc.__class__.__name__}", type="api_error")
        return JSONResponse(status_code=500, content=error.to_dict())
