"""异常类型与 Flask 异常处理器：所有出口都走统一信封。"""

from __future__ import annotations

from collections.abc import Mapping

from flask import Flask, Response
from werkzeug.exceptions import HTTPException

from zhyonlineedu.core.codes import (
    ACCESS_LEVEL_REQUIRED,
    AUTH_TOKEN_EXPIRED,
    INTERNAL_ERROR,
    METHOD_NOT_ALLOWED,
    NOT_FOUND,
    VALIDATION_FAILED,
    Code,
)
from zhyonlineedu.core.envelope import fail


class AppError(Exception):
    """业务异常：携带附录 A 错误码与可选 detail。"""

    def __init__(
        self,
        code: Code | str,
        *,
        data: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.code = code
        self.data = data
        self.headers = dict(headers or {})
        super().__init__(str(code))


_HTTP_CODE: Mapping[int, Code | str] = {
    400: VALIDATION_FAILED,
    401: AUTH_TOKEN_EXPIRED,
    403: ACCESS_LEVEL_REQUIRED,
    404: NOT_FOUND,
    405: METHOD_NOT_ALLOWED,
    422: VALIDATION_FAILED,
    429: Code.RATE_LIMITED,
}


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(AppError)
    def handle_app_error(exc: AppError) -> tuple[Response, int]:
        return fail(exc.code, data=exc.data, headers=exc.headers)

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException) -> tuple[Response, int]:
        status = exc.code if isinstance(exc.code, int) else 500
        code = _HTTP_CODE.get(status)
        if code is None:
            app.logger.warning("未映射的 HTTP 异常 status=%s path=%s", status, exc.description)
            code = INTERNAL_ERROR
        return fail(code, data={"http_status": status})

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception) -> tuple[Response, int]:
        app.logger.error("未捕获异常", exc_info=exc)
        return fail(INTERNAL_ERROR)
