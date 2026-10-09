"""
统一异常与全局异常处理器

所有对外错误响应保持一致结构：
    {"error": {"message": str, "code": str | None, "status": int}}
避免前端猜测错误体格式。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppException(Exception):
    """业务异常基类。子类可固定 status_code / error_code。"""

    status_code: int = 500
    error_code: str = "APP_ERROR"

    def __init__(self, message: str, *, status_code: int | None = None,
                 error_code: str | None = None):
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if error_code is not None:
            self.error_code = error_code
        super().__init__(message)


class NotFoundError(AppException):
    status_code = 404
    error_code = "NOT_FOUND"


class UnauthorizedError(AppException):
    status_code = 401
    error_code = "UNAUTHORIZED"


class ForbiddenError(AppException):
    status_code = 403
    error_code = "FORBIDDEN"


def _error_response(status: int, message: str, code: str | None,
                    details: object | None = None) -> JSONResponse:
    payload = {"error": {"message": message, "code": code, "status": status}}
    if details is not None:
        payload["error"]["details"] = details
    return JSONResponse(status_code=status, content=payload)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def _app_exception_handler(request: Request, exc: AppException):
        return _error_response(exc.status_code, exc.message, exc.error_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        return _error_response(422, "Validation failed", "VALIDATION_ERROR",
                               details=exc.errors())

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return _error_response(500, "Internal server error", "INTERNAL_ERROR")
