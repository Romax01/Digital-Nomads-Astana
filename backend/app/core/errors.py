"""Единый формат ошибок: {"error": {"code", "message", "details", "hint"}}.

code — стабильный машинный идентификатор, message — понятное объяснение на русском.
"""
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    status_code = 400

    def __init__(self, code: str, message: str, *, status: int | None = None,
                 details: Any = None, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.hint = hint
        if status:
            self.status_code = status

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message,
                          "details": self.details, "hint": self.hint}}


class NotFound(AppError):
    status_code = 404


class Forbidden(AppError):
    status_code = 403


class Conflict(AppError):
    status_code = 409


class Unauthorized(AppError):
    status_code = 401


_FIELD_MESSAGES = {
    "missing": "обязательное поле не заполнено",
    "int_parsing": "ожидается целое число",
    "float_parsing": "ожидается число",
    "greater_than": "значение должно быть больше {gt}",
    "greater_than_equal": "значение должно быть не меньше {ge}",
    "less_than_equal": "значение должно быть не больше {le}",
    "datetime_parsing": "ожидается дата и время в формате ISO 8601 с часовым поясом",
    "datetime_from_date_parsing": "ожидается дата и время в формате ISO 8601",
    "string_too_short": "слишком короткое значение",
    "string_too_long": "слишком длинное значение",
    "literal_error": "недопустимое значение",
    "enum": "недопустимое значение",
}


async def app_error_handler(_: Request, exc: AppError):
    return JSONResponse(status_code=exc.status_code, content=exc.body())


async def validation_error_handler(_: Request, exc: RequestValidationError):
    fields = []
    for e in exc.errors():
        tpl = _FIELD_MESSAGES.get(e.get("type", ""), "некорректное значение")
        try:
            msg = tpl.format(**(e.get("ctx") or {}))
        except (KeyError, IndexError):
            msg = tpl
        fields.append({"field": ".".join(str(p) for p in e.get("loc", []) if p != "body"),
                       "message": msg})
    return JSONResponse(status_code=422, content={"error": {
        "code": "VALIDATION_ERROR",
        "message": "Запрос содержит некорректные данные.",
        "details": {"fields": fields},
        "hint": "Исправьте отмеченные поля и повторите запрос."}})


async def http_error_handler(_: Request, exc: StarletteHTTPException):
    messages = {404: ("NOT_FOUND", "Ресурс не найден."),
                405: ("METHOD_NOT_ALLOWED", "Метод не поддерживается."),
                401: ("UNAUTHORIZED", "Требуется вход в систему.")}
    code, msg = messages.get(exc.status_code, ("HTTP_ERROR", str(exc.detail)))
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"code": code, "message": msg, "details": None, "hint": None}})


async def unhandled_error_handler(_: Request, exc: Exception):
    import logging
    logging.getLogger("app").exception("Необработанная ошибка: %s", exc)
    return JSONResponse(status_code=500, content={"error": {
        "code": "INTERNAL_ERROR", "message": "Внутренняя ошибка сервера. Действие не выполнено.",
        "details": None, "hint": "Повторите попытку. Если ошибка повторяется — см. журнал backend."}})
