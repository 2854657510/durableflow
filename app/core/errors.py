from __future__ import annotations


class AppError(Exception):
    """带业务错误码的异常，统一由 main.py 转成错误响应。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict | list | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
