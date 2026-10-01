"""Прогноз операций: распространение задержек по технологической цепочке.

Правило: операция не начинается раньше планового начала, раньше «не ранее» (прибытие поезда
к входному сигналу, расписание отправления), раньше окончания предыдущей операции и раньше
текущего модельного времени. Длительность увеличивается на задержку от инцидентов.
Прогноз не учитывает перестановки — это задача планировщика; пересечения, возникающие из-за
задержек, показывает детектор конфликтов.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core.timeutil import aware
from app.services.model import StationModel


def forecast(model: StationModel) -> dict[str, tuple[datetime, datetime]]:
    now = model.now
    out: dict[str, tuple[datetime, datetime]] = {}
    chains = list(model.ops_by_train.items())
    standalone = [o for o in model.ops.values() if not o.train_id]
    for train_id, ops in chains:
        train = model.trains.get(train_id)
        t_prev = None
        for o in ops:
            dur = timedelta(minutes=o.duration_min + (o.extra_delay_min or 0))
            if o.status == "cancelled":
                continue
            if o.status == "done":
                s, e = aware(o.actual_start or o.planned_start), aware(o.actual_end or o.planned_end)
            elif o.status == "in_progress":
                s = aware(o.actual_start or o.planned_start)
                e = max(s + dur, now)
            else:
                cands = [aware(o.planned_start), now]
                if o.not_before:
                    cands.append(aware(o.not_before))
                if o.kind == "arrival" and train and train.expected_arrival:
                    cands.append(aware(train.expected_arrival))
                if t_prev:
                    cands.append(t_prev)
                s = max(cands)
                e = s + dur
            out[o.id] = (s, e)
            t_prev = e
    for o in standalone:
        dur = timedelta(minutes=o.duration_min + (o.extra_delay_min or 0))
        if o.status == "done":
            out[o.id] = (aware(o.actual_start or o.planned_start), aware(o.actual_end or o.planned_end))
        elif o.status == "in_progress":
            s = aware(o.actual_start)
            out[o.id] = (s, max(s + dur, now))
        elif o.status != "cancelled":
            s = max([aware(o.planned_start), now] + ([aware(o.not_before)] if o.not_before else []))
            out[o.id] = (s, s + dur)
    return out


def departure_delays(model: StationModel, fc: dict) -> dict[str, float]:
    """Задержка отправления относительно расписания, мин (по прогнозу)."""
    out = {}
    for tid, ops in model.ops_by_train.items():
        t = model.trains[tid]
        dep = next((o for o in ops if o.kind == "departure" and o.status != "cancelled"), None)
        if dep and t.scheduled_departure and dep.id in fc:
            out[tid] = max(0.0, (fc[dep.id][0] - aware(t.scheduled_departure)).total_seconds() / 60)
        arr = next((o for o in ops if o.kind == "arrival"), None)
        if not dep and arr and t.scheduled_arrival and arr.id in fc:
            out[tid] = max(0.0, (fc[arr.id][0] - aware(t.scheduled_arrival)).total_seconds() / 60)
    return out
