"""Метрики задержек по этапам обработки (скользящее окно), p50/p95/max.

Этапы: приём телеметрии, сохранение, обновление модели, доставка клиенту, отрисовка
(сообщается клиентом), расчёт плана, время ответа API.
"""
import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_samples: dict[str, deque] = defaultdict(lambda: deque(maxlen=5000))
_counters: dict[str, int] = defaultdict(int)
_started = time.time()

STAGE_LABELS = {
    "telemetry_transport_ms": "Передача: измерение → приём backend",
    "telemetry_persist_ms": "Проверка и сохранение телеметрии",
    "model_update_ms": "Обновление модели станции после события",
    "ws_delivery_ms": "Доставка обновления клиенту (backend)",
    "event_to_ws_ms": "Событие в backend → отправка в браузер",
    "client_render_ms": "Отображение в браузере после получения",
    "event_to_screen_ms": "Событие в backend → видимое обновление",
    "plan_compute_ms": "Расчёт плана (CP-SAT)",
    "check_ms": "Проверка заявки",
    "api_ms": "Время ответа API",
    "state_build_ms": "Сборка состояния станции",
}


def observe(name: str, value_ms: float):
    with _lock:
        _samples[name].append(float(value_ms))


def incr(name: str, n: int = 1):
    with _lock:
        _counters[name] += n


def _pct(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return round(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f), 2)


def snapshot() -> dict:
    with _lock:
        stages = {}
        for name, dq in _samples.items():
            vals = sorted(dq)
            stages[name] = {"label": STAGE_LABELS.get(name, name), "count": len(vals),
                            "p50": _pct(vals, 0.5), "p95": _pct(vals, 0.95),
                            "max": round(vals[-1], 2) if vals else None}
        return {"uptime_s": round(time.time() - _started, 1), "stages": stages, "counters": dict(_counters)}


def reset():
    with _lock:
        _samples.clear()
        _counters.clear()
