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


from app.api.routes import router as api_router  # noqa: E402
from app.api.ws import router as ws_router  # noqa: E402

app.include_router(api_router)
app.include_router(ws_router)
