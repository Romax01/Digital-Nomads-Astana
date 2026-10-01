"""Интегральный индекс эффективности станции (0–100).

I = 100 × Σ(wᵢ·sᵢ) / Σwᵢ по составляющим с доступными данными; sᵢ ∈ [0, 1].
Составляющие без данных или с устаревшими данными исключаются из числителя И знаменателя,
а качество оценки (доля веса с данными) показывается явно — благоприятные значения вместо
отсутствующих измерений не подставляются.

Индекс — аналитический показатель. Он не является разрешением на движение: жёсткие
ограничения проверяются независимо от его значения.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import aware, iso, utcnow
from app.models import IndexConfig

COMPONENTS = {
    "throughput": {"title": "Пропускная способность", "unit": "ваг./ч",
                   "source": "Журнал операций: завершённые техосмотры (обработанные вагоны)"},
    "schedule_deviation": {"title": "Среднее отклонение от графика", "unit": "мин",
                           "source": "Отправления: факт и прогноз относительно расписания"},
    "track_utilization": {"title": "Загрузка путей приёмо-отправочного парка", "unit": "доля путей",
                          "source": "Телеметрия рельсовых цепей (только актуальные данные)"},
    "route_conflicts": {"title": "Конфликты маршрутов и занятости", "unit": "шт.",
                        "source": "Детектор конфликтов прогнозного плана"},
    "idle": {"title": "Простой локомотивов и бригад сверх резерва", "unit": "доля смены",
             "source": "Журнал операций и смены ресурсов"},
}
IDLE_KINDS = ("shunting_loco", "loco_crew", "shunting_crew")


def active_config(db: Session) -> IndexConfig:
    return db.execute(select(IndexConfig).where(IndexConfig.active.is_(True)).order_by(IndexConfig.version.desc())).scalars().first()


def validate_config(cfg: dict) -> list[str]:
    errs = []
    w = cfg.get("weights", {})
    if set(w) != set(COMPONENTS):
        errs.append("Веса должны быть заданы для всех составляющих: " + ", ".join(COMPONENTS))
    if any((not isinstance(v, (int, float))) or v < 0 for v in w.values()):
        errs.append("Веса должны быть неотрицательными числами.")
    if sum(v for v in w.values() if isinstance(v, (int, float))) <= 0:
        errs.append("Сумма весов должна быть больше нуля.")
    th = cfg.get("thresholds", {})
    if not (0 <= th.get("attention", -1) < th.get("normal", -1) <= 100):
        errs.append("Пороги: 0 ≤ «Внимание» < «Норма» ≤ 100.")
    tu = cfg.get("params", {}).get("track_utilization", {})
    if tu and not (0 <= tu.get("zero_at_low", 0) < tu.get("target_low", 0) <= tu.get("target_high", 0) < tu.get("zero_at_high", 0) <= 1.5):
        errs.append("Загрузка путей: 0 ≤ нижний ноль < нижняя граница ≤ верхняя граница < верхний ноль.")
    rs = cfg.get("params", {}).get("idle", {}).get("reserve_share", 0.2)
    if not 0 <= rs < 1:
        errs.append("Необходимый резерв ресурсов должен быть в диапазоне [0; 1).")
    return errs


def utilization_score(u: float, p: dict) -> float:
    lo, hi = p["target_low"], p["target_high"]
    z0, z1 = p.get("zero_at_low", 0.0), p.get("zero_at_high", 1.0)
    if lo <= u <= hi:
        return 1.0
    if u < lo:
        return max(0.0, (u - z0) / (lo - z0)) if lo > z0 else 0.0
    return max(0.0, (z1 - u) / (z1 - hi)) if z1 > hi else 0.0


def category(value: float | None, th: dict) -> str:
    if value is None:
        return "unknown"
    if value >= th["normal"]:
        return "normal"
    if value >= th["attention"]:
        return "attention"
    return "critical"


CATEGORY_LABEL = {"normal": "Норма", "attention": "Внимание", "critical": "Критично", "unknown": "Нет оценки"}


def _aggregate(cfg: dict, comps: dict) -> dict:
    w = cfg["weights"]
    total_w = sum(w.values())
    avail = {k: c for k, c in comps.items() if c.get("score") is not None}
    aw = sum(w[k] for k in avail)
    value = round(100 * sum(w[k] * c["score"] for k, c in avail.items()) / aw, 1) if aw > 0 else None
    coverage = aw / total_w if total_w else 0
    factors = sorted(([k, w[k] * (1 - c["score"]) / total_w * 100] for k, c in avail.items()), key=lambda kv: -kv[1])
    cat = category(value, cfg["thresholds"])
    quality = "full" if coverage >= 0.999 else ("partial" if coverage >= 0.6 else "low")
    return {"value": value, "category": cat, "category_label": CATEGORY_LABEL[cat],
            "quality": {"level": quality, "coverage": round(coverage, 3),
                        "label": {"full": "Полная", "partial": "Частичная", "low": "Низкая"}[quality],
                        "missing": [COMPONENTS[k]["title"] for k, c in comps.items() if c.get("score") is None]},
            "components": comps,
            "factors": [{"key": k, "title": COMPONENTS[k]["title"], "loss_points": round(v, 1),
                         "explanation": comps[k]["explanation"]} for k, v in factors if v >= 0.5][:3]}


def compute_current(db: Session, model, conflicts: list[dict]) -> dict:
    ic = active_config(db)
    cfg = ic.config
    p = cfg["params"]
    now = model.now
    comps = {}
    # 1. пропускная способность
    win = timedelta(minutes=p["throughput"]["window_min"])
    wagons = 0
    for tid, ops in model.ops_by_train.items():
        t = model.trains[tid]
        for o in ops:
            if o.kind == "inspection" and o.status == "done" and o.actual_end and now - win <= aware(o.actual_end) <= now:
                wagons += t.wagons_count
    rate = wagons / (win.total_seconds() / 3600)
    target = p["throughput"]["target_wagons_per_hour"]
    s1 = min(1.0, rate / target) if target else None
    comps["throughput"] = _c("throughput", round(rate, 1), s1, f"{rate:.0f} ваг./ч при цели {target} ваг./ч "
                             f"за последние {p['throughput']['window_min']} мин", p["throughput"]["window_min"])
    # 2. отклонение от графика
    win2 = timedelta(minutes=p["schedule_deviation"]["window_min"])
    delays = []
    from app.services.forecast import forecast
    fc = model._fc if hasattr(model, "_fc") else forecast(model)
    for tid, ops in model.ops_by_train.items():
        t = model.trains[tid]
        dep = next((o for o in ops if o.kind == "departure"), None)
        if not dep or not t.scheduled_departure:
            continue
        sched = aware(t.scheduled_departure)
        if dep.status == "done" and dep.actual_start and now - win2 <= aware(dep.actual_start) <= now:
            delays.append(max(0, (aware(dep.actual_start) - sched).total_seconds() / 60))
        elif dep.status != "done" and dep.id in fc and fc[dep.id][0] <= now + timedelta(hours=1):
            delays.append(max(0, (fc[dep.id][0] - sched).total_seconds() / 60))
    if delays:
        avg = sum(delays) / len(delays)
        s2 = max(0.0, 1 - avg / p["schedule_deviation"]["max_avg_delay_min"])
        comps["schedule_deviation"] = _c("schedule_deviation", round(avg, 1), s2,
                                         f"среднее {avg:.0f} мин по {len(delays)} отправлениям (факт и прогноз на 1 ч)",
                                         p["schedule_deviation"]["window_min"])
    else:
        comps["schedule_deviation"] = _c("schedule_deviation", None, None, "нет отправлений в окне — нет данных",
                                         p["schedule_deviation"]["window_min"])
    # 3. загрузка путей по актуальной телеметрии
    rd = [tid for tid, t in model.track_rows.items() if t.kind == "receiving_departure"]
    known = [tid for tid in rd if model.data_states.get(tid, {}).get("state") == "actual"]
    max_age = cfg.get("max_data_age_s", 30)
    if known and len(known) >= len(rd) * 0.5:
        occ = sum(1 for tid in known if model.data_states[tid].get("observed") == "occupied")
        u = occ / len(known)
        s3 = utilization_score(u, p["track_utilization"])
        note = "" if len(known) == len(rd) else f"; без данных: {len(rd) - len(known)} путь(и) исключены"
        comps["track_utilization"] = _c("track_utilization", round(u, 3), s3,
                                        f"занято {occ} из {len(known)} путей ({u:.0%}), целевой диапазон "
                                        f"{p['track_utilization']['target_low']:.0%}–{p['track_utilization']['target_high']:.0%}{note}", 0)
    else:
        comps["track_utilization"] = _c("track_utilization", None, None,
                                        f"актуальные данные телеметрии есть только по {len(known)} из {len(rd)} путей "
                                        f"(порог устаревания до {max_age} с) — оценка не выполняется", 0)
    # 4. конфликты
    from app.services.conflicts import route_conflict_count
    n = route_conflict_count(conflicts)
    mx = p["route_conflicts"]["max_conflicts"]
    comps["route_conflicts"] = _c("route_conflicts", n, max(0.0, 1 - n / mx), f"{n} конфликт(ов) в прогнозе (0 баллов при {mx})", 0)
    # 5. простой сверх резерва
    win5 = timedelta(minutes=p["idle"]["window_min"])
    t0 = now - win5
    shift_min = busy_min = 0.0
    for rid, r in model.resources.items():
        if r.kind not in IDLE_KINDS:
            continue
        sh = model.shifts.get(rid) or [(t0, now)]
        for a, b in sh:
            a2, b2 = max(a, t0), min(b, now)
            if b2 > a2:
                shift_min += (b2 - a2).total_seconds() / 60
        for o in model.ops.values():
            if rid in (o.resource_ids or []) and o.actual_start:
                a2 = max(aware(o.actual_start), t0)
                b2 = min(aware(o.actual_end) if o.actual_end else now, now)
                if b2 > a2:
                    busy_min += (b2 - a2).total_seconds() / 60
    if shift_min > 0:
        idle = max(0.0, 1 - busy_min / shift_min)
        rs = p["idle"]["reserve_share"]
        excess = max(0.0, idle - rs)
        s5 = max(0.0, 1 - excess / (1 - rs))
        comps["idle"] = _c("idle", round(idle, 3), s5, f"простой {idle:.0%} смены при необходимом резерве {rs:.0%} "
                           f"(сверх резерва {excess:.0%})", p["idle"]["window_min"])
    else:
        comps["idle"] = _c("idle", None, None, "нет данных о сменах ресурсов в окне", p["idle"]["window_min"])
    out = _aggregate(cfg, comps)
    out.update({"computed_at": iso(now), "computed_real_at": iso(utcnow()), "config_version": ic.version,
                "mode": "current", "formula": "I = 100 × Σ(wᵢ·sᵢ) / Σwᵢ",
                "weights": cfg["weights"], "thresholds": cfg["thresholds"]})
    return out


def _c(key, raw, score, explanation, window_min):
    return {"key": key, "title": COMPONENTS[key]["title"], "unit": COMPONENTS[key]["unit"],
            "source": COMPONENTS[key]["source"], "raw": raw, "score": None if score is None else round(score, 3),
            "explanation": explanation, "window_min": window_min}


def compute_plan_index(model, schedule: dict | None, kpis: dict) -> dict:
    """Прогноз индекса для плана на ближайшие 4 ч (по расчётным, а не наблюдаемым данным)."""
    from app.db import SessionLocal
    with SessionLocal() as db:
        ic = active_config(db)
    cfg = ic.config
    p = cfg["params"]
    now = model.now
    h = timedelta(hours=4)
    comps = {}
    wagons = 0
    for tid, ops in model.ops_by_train.items():
        t = model.trains[tid]
        for o in ops:
            if o.kind != "inspection" or o.status == "done":
                continue
            end = schedule[o.id]["end"] if schedule and o.id in schedule else aware(o.forecast_end or o.planned_end)
            if now <= end <= now + h:
                wagons += t.wagons_count
    rate = wagons / 4
    target = p["throughput"]["target_wagons_per_hour"]
    comps["throughput"] = _c("throughput", round(rate, 1), min(1.0, rate / target) if target else None,
                             f"план: {rate:.0f} ваг./ч на 4 ч", 240)
    n_dep = max(1, kpis.get("delayed_trains", 0) + 1)
    avg = kpis.get("total_delay_min", 0) / max(1, _count_departures(model, schedule))
    comps["schedule_deviation"] = _c("schedule_deviation", round(avg, 1),
                                     max(0.0, 1 - avg / p["schedule_deviation"]["max_avg_delay_min"]),
                                     f"план: средняя задержка {avg:.0f} мин", 240)
    u = kpis.get("rd_utilization_4h", 0)
    comps["track_utilization"] = _c("track_utilization", u, utilization_score(u, p["track_utilization"]),
                                    f"план: загрузка парка {u:.0%} за 4 ч", 240)
    n = kpis.get("conflicts", 0)
    comps["route_conflicts"] = _c("route_conflicts", n, max(0.0, 1 - n / p["route_conflicts"]["max_conflicts"]),
                                  f"план: {n} конфликт(ов)", 0)
    comps["idle"] = _c("idle", None, None, "простой в прогнозе не оценивается", 0)
    out = _aggregate(cfg, comps)
    out.update({"mode": "forecast", "config_version": ic.version, "computed_at": iso(now)})
    _ = n_dep
    return out


def _count_departures(model, schedule) -> int:
    n = 0
    for tid, ops in model.ops_by_train.items():
        if any(o.kind == "departure" and o.status != "done" and (schedule is None or o.id in schedule) for o in ops):
            n += 1
    return n
