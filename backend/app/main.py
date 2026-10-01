"""Точка входа backend «Цифровая станция» (демонстрационный MVP системы поддержки решений).

Не является сертифицированной системой обеспечения безопасности движения и не управляет
реальными стрелками, сигналами и поездами.
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.core import metrics
from app.core.errors import (AppError, app_error_handler, http_error_handler, unhandled_error_handler,
                             validation_error_handler)


class JsonFormatter(logging.Formatter):
    def format(self, record):
        d = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname, "logger": record.name,
             "msg": record.getMessage()}
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


def setup_logging():
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [h]
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    task = None
    if s.engine_enabled:
        from app.iot.runtime import ingest_service
        from app.services.hub import hub
        ingest_service.start()
        hub.replanner.start()
        task = asyncio.create_task(hub.run())
        logging.getLogger("app").info("Запущены: приём телеметрии, перепланирование, realtime-цикл")
    yield
    if task:
        from app.services.hub import hub
        hub.running = False
        task.cancel()


TAGS = [
    {"name": "Доступ", "description": "Вход и права. Ролевая модель MVP — допущение, требует проверки специалистом."},
    {"name": "Состояние", "description": "Текущее состояние, топология и карточки объектов."},
    {"name": "Заявки", "description": "Заявки между станциями: проверка, подтверждение, отказ, перенос, альтернативы."},
    {"name": "Планирование", "description": "Планировщик CP-SAT, версии планов, рекомендации, ручной перенос."},
    {"name": "Инциденты", "description": "Нештатные ситуации и их устранение."},
    {"name": "IoT", "description": "Реестр устройств, телеметрия, качество данных, ручное уточнение."},
    {"name": "Индекс", "description": "Индекс эффективности станции и его конфигурация."},
    {"name": "Симуляция", "description": "Модельное время и демонстрационные сценарии."},
    {"name": "История", "description": "Перемотка: снимки и дельты состояния."},
]

app = FastAPI(
    title="Цифровая станция — API (демо)",
    version="1.0.0",
    description="Цифровой двойник железнодорожной станции: состояние, проверка ограничений, планирование, IoT. "
                "Демонстрационная система поддержки решений на синтетических данных; не управляет движением. "
                "Ошибки возвращаются в едином формате {error: {code, message, details, hint}}.",
    openapi_tags=TAGS,
    lifespan=lifespan,
    # стандартные /docs, /redoc и /openapi.json отключены: описание API доступно только администратору
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(GZipMiddleware, minimum_size=2000)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_methods=["*"], allow_headers=["*"])
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(StarletteHTTPException, http_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)


@app.middleware("http")
async def timing(request: Request, call_next):
    t0 = time.perf_counter()
    resp = await call_next(request)
    ms = (time.perf_counter() - t0) * 1000
    if request.url.path.startswith("/api/") and not request.url.path.endswith("/world"):
        metrics.observe("api_ms", ms)
    resp.headers["X-Response-Time-ms"] = f"{ms:.1f}"
    return resp


from fastapi.openapi.docs import get_swagger_ui_html  # noqa: E402
from fastapi.openapi.utils import get_openapi  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

DOCS_COOKIE = "ds_docs"


def _docs_allowed(request: Request) -> bool:
    """Описание API — только для роли с правом api.docs (администратор). Токен берётся из
    HttpOnly-cookie, выдаваемой POST /api/v1/auth/docs-session, или из заголовка Authorization."""
    from app.core.permissions import can
    from app.core.security import user_from_token
    from app.db import SessionLocal
    token = request.cookies.get(DOCS_COOKIE)
    auth = request.headers.get("authorization", "")
    if not token and auth.lower().startswith("bearer "):
        token = auth[7:]
    if not token:
        return False
    try:
        with SessionLocal() as db:
            return can(user_from_token(db, token), "api.docs")
    except Exception:
        return False


@app.get("/docs", include_in_schema=False)
def swagger_ui(request: Request):
    if not _docs_allowed(request):
        return HTMLResponse(status_code=403, content=(
            "<!doctype html><html lang='ru'><meta charset='utf-8'><title>Доступ запрещён</title>"
            "<body style='font-family:system-ui;background:#0d141c;color:#e7edf3;padding:40px'>"
            "<h1>Описание API недоступно</h1><p>Swagger открыт только для администратора стенда. "
            "Войдите в интерфейс под ролью «Администратор» и откройте «API (Swagger)» из меню.</p>"
            "<p><a style='color:#5aa9ff' href='/'>Вернуться в интерфейс</a></p></body></html>"))
    return get_swagger_ui_html(openapi_url="/openapi.json", title="Цифровая станция — API (демо)")


@app.get("/openapi.json", include_in_schema=False)
def openapi_json(request: Request):
    if not _docs_allowed(request):
        return JSONResponse(status_code=403, content={"error": {
            "code": "FORBIDDEN", "message": "Описание API доступно только администратору.", "details": None,
            "hint": "Войдите под ролью «Администратор»."}})
    if app.openapi_schema is None:
        app.openapi_schema = get_openapi(title=app.title, version=app.version, description=app.description,
                                         routes=app.routes, tags=TAGS)
    return JSONResponse(app.openapi_schema)


from app.api.routes import router as api_router  # noqa: E402
from app.api.ws import router as ws_router  # noqa: E402

app.include_router(api_router)
app.include_router(ws_router)
