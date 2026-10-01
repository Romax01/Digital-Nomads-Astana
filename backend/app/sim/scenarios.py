"""Демонстрационные сценарии.

Сценарий = базовое расписание (seed) + фикстуры + запланированные события модельного
времени. События меняют исходные данные (инцидент, неисправность устройства в симуляторе),
а последствия — конфликты, блокировки, рекомендации — вычисляются системой.
"""
from __future__ import annotations

import random
from datetime import timedelta

from sqlalchemy.orm import Session

from app.models import Plan, SimState
from app.sim.seed import demo_request, local_day

SCENARIOS = {
    "normal": {"title": "Обычная работа", "description": "Типовое расписание дня, заявка Z-0001 на 40 вагонов от станции Отар."},
    "demo_plan_exceeded": {"title": "Заявка: план выполнен, место есть",
                           "description": "Месячный план уже выполнен, физическая возможность приёма есть. Проверка "
                                          "предупреждает о превышении и применяет политику (цель или жёсткая квота)."},
    "demo_tracks_busy": {"title": "Заявка: пути заняты на время прибытия",
                         "description": "План не выполнен, но все подходящие пути заняты на интервал приёма. "
                                          "Подтверждение запрещено, рассчитывается ближайшее окно и альтернативы."},
    "demo_track_frees": {"title": "Заявка: сейчас заполнено, к прибытию освободится",
                         "description": "Все пути парка сейчас заняты, но к прогнозируемому прибытию один путь "
                                        "освобождается. Проверяется весь интервал — приём допускается."},
    "peak_arrivals": {"title": "Пик прибытий", "description": "Шесть дополнительных поездов прибывают в течение полутора часов."},
    "track_closure": {"title": "Закрытие пути", "description": "Через 10 минут модельного времени закрывается путь с запланированными операциями."},
    "cargo_delay": {"title": "Задержка грузовой операции", "description": "Выгрузка на грузовом фронте задерживается на 60 минут."},
    "faulty_wagon": {"title": "Неисправный вагон", "description": "Диагностический комплекс ПТО обнаруживает неисправный вагон при осмотре."},
    "neighbor_restriction": {"title": "Ограничение соседней станции", "description": "Жетыген (демо) ограничивает приём с 12:20 до 15:00."},
    "recovery": {"title": "Восстановление после инцидента", "description": "Путь закрывается на 40 минут, затем работа восстанавливается."},
    "sensor_loss": {"title": "Потеря связи с датчиком", "description": "Рельсовая цепь пути 3 перестаёт передавать данные на 60 с, затем перезапускается."},
    "iot_e2e": {"title": "Сквозной сценарий IoT и перепланирования",
                "description": "Заявка Z-0001 → подтверждение → инцидент на запланированном пути → конфликт → альтернативы → решение → аудит → перемотка."},
    "multi_5": {"title": "5 одновременных нештатных ситуаций", "description": "Нагрузочный сценарий для пересчёта плана."},
    "multi_10": {"title": "10 одновременных нештатных ситуаций", "description": "Нагрузочный сценарий для пересчёта плана."},
}


def _ev(t0, minutes, **kw):
    return {"at": (t0 + timedelta(minutes=minutes)).isoformat(), "fired": False, **kw}


def apply_scenario(db: Session, tb, cfg: dict, scenario: str, rnd: random.Random):
    sim = db.get(SimState, 1)
    t0 = tb.t0
    sid = cfg["station"]["id"]
    tt = cfg.get("timetable", {})
    counts = {k: tt.get(k, 0) for k in ("passenger", "transit", "transfer_in", "cargo")}
    span = tt.get("span_h", 10)
    rd = [t for t in tb.model.track_rows if tb.model.track_rows[t].kind == "receiving_departure"]
    T = lambda n: f"{sid}-T{n}"  # noqa: E731
    day = local_day(cfg)
    events = []

    def at_local(h):
        return (day + timedelta(hours=h)).astimezone(t0.tzinfo)

    def restrict_random(avoid: list[str]):
        allowed = [t for t in rd if t not in avoid]
        orig = tb.add

        def add(tpl, arrival, **kw):
            if tpl in ("transit", "transfer_in", "cargo") and "fixed_tracks" not in kw:
                kw["fixed_tracks"] = allowed
            return orig(tpl, arrival, **kw)
        tb.add = add

    big = sid == "ALM"
    if scenario == "demo_plan_exceeded":
        plan = db.query(Plan).first()
        plan.actual_base_wagons = plan.target_wagons
        restrict_random([T(4)] if big else [T(1)])
        tb.random_timetable(counts, span)
    elif scenario == "demo_tracks_busy":
        if big:
            fixtures = [(3, 13.6, 45, 95), (4, 13.75, 52, 120), (5, 13.9, 50, 130), (1, 14.0, 48, 90), (2, 14.2, 47, 85)]
        else:
            fixtures = [(1, 13.9, 44, 100), (2, 14.0, 40, 95), (3, 14.1, 36, 80)]
        for n, h, w, dwell in fixtures:
            tb.add("transit", at_local(h), wagons=w, side_in="east", dwell_min=dwell, exact=False,
                   fixed_tracks=[T(n)], wagon_kinds=["gondola"])
        tb.random_timetable(counts, span)
    elif scenario == "demo_track_frees":
        if big:
            fixtures = [(1, 10.6, 48, 330), (2, 10.9, 47, 120), (3, 11.0, 44, 320), (4, 11.2, 52, 300),
                        (5, 11.4, 50, 310), (6, 11.5, 30, 300)]
        else:
            fixtures = [(1, 10.8, 44, 330), (2, 11.0, 40, 110), (3, 11.2, 36, 320)]
        for n, h, w, dwell in fixtures:
            tb.add("transit", at_local(h), wagons=w, side_in="east", dwell_min=dwell, exact=False,
                   fixed_tracks=[T(n)], wagon_kinds=["gondola"])
        restrict_random([T(2)])
        tb.random_timetable({"passenger": counts["passenger"], "cargo": counts["cargo"]}, span)
    elif scenario == "peak_arrivals":
        tb.random_timetable(counts, span, cluster=("transit", 6, 0.5, 2.0))
    else:
        tb.random_timetable(counts, span)

    tb.finalize()
    demo_request(db, cfg, status="new")

    if scenario == "track_closure":
        events.append(_ev(t0, 10, type="incident", kind="track_closure", target="busiest_rd_track", duration_min=150))
    elif scenario == "cargo_delay":
        events.append(_ev(t0, 8, type="incident", kind="cargo_delay", target="active_cargo", extra_min=60))
    elif scenario == "faulty_wagon":
        events.append(_ev(t0, 3, type="world", key="diag_faulty_next", value=True))
    elif scenario == "neighbor_restriction":
        east = next((n["id"] for n in cfg["neighbors"] if n["side"] == "east"), None)
        events.append(_ev(t0, 5, type="incident", kind="neighbor_restriction", target=east,
                          start_local_h=12.33, duration_min=160,
                          title=f"Ограничение приёма: {next(n['name'] for n in cfg['neighbors'] if n['id'] == east)} не принимает поезда"))
    elif scenario == "recovery":
        events.append(_ev(t0, 5, type="incident", kind="track_closure", target="busiest_rd_track", duration_min=40))
    elif scenario in ("sensor_loss",):
        events.append(_ev(t0, 3, type="device_fault", device_id=f"{sid}-TC-3", mode="silent", duration_s=60,
                          then="reboot"))
    elif scenario == "iot_e2e":
        events.append(_ev(t0, 30, type="incident", kind="track_closure", target="request_track:Z-0001",
                          duration_min=180, requires="request_confirmed:Z-0001",
                          title_tpl="Закрытие пути: излом рельса (демо)"))
        events.append(_ev(t0, 50, type="device_fault", device_id=f"{sid}-TC-1", mode="silent", duration_s=45,
                          then="reboot"))
    elif scenario in ("multi_5", "multi_10"):
        n = 5 if scenario == "multi_5" else 10
        pool = [
            dict(kind="track_closure", target="rd_track:0", duration_min=120),
            dict(kind="cargo_delay", target="active_cargo", extra_min=50),
            dict(kind="resource_failure", target="res:IT-1", duration_min=90),
            dict(kind="neighbor_restriction", target="east_neighbor", duration_min=90),
            dict(kind="track_closure", target="rd_track:1", duration_min=90),
            dict(kind="resource_failure", target="res:ML-1", duration_min=60),
            dict(kind="faulty_wagon", target="onstation_wagon"),
            dict(kind="track_closure", target="sorting_track:0", duration_min=120),
            dict(kind="resource_failure", target="res:LB-1", duration_min=60),
            dict(kind="track_closure", target="rd_track:2", duration_min=60),
        ]
        for i, p in enumerate(pool[:n]):
            events.append(_ev(t0, 2, type="incident", **p, batch=scenario))
    sim.scheduled_events = events
