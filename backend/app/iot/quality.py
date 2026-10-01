"""Качество данных телеметрии.

Состояния данных: actual (актуальные), stale (устаревшие), missing (отсутствующие),
contradictory (противоречивые), invalid (некорректные). Состояние подключения устройства
(по heartbeat) — отдельный признак.

Ключевое правило: устаревшие или отсутствующие данные никогда не трактуются как «свободно»
или «исправно». Путь без достоверных данных получает статус «Неизвестно», и подтверждение
зависящих от него операций блокируется до получения достаточных данных.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import aware, local_hm
from app.models import Device, ManualOverride, Observation

STATE_LABELS = {
    "actual": "Актуальные", "stale": "Устаревшие", "missing": "Отсутствуют",
    "contradictory": "Противоречивые", "invalid": "Некорректные", "not_monitored": "Без датчика",
    "override": "Ручное уточнение",
}

CONTRADICTION_GRACE_S = 6.0  # расхождение короче этого считается задержкой доставки
_mismatch_since: dict[str, float] = {}
_lock = threading.Lock()


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def ago(seconds: float) -> str:
    s = int(round(seconds))
    if s < 120:
        return f"{s} {plural(s, 'секунду', 'секунды', 'секунд')} назад"
    m = s // 60
    return f"{m} {plural(m, 'минуту', 'минуты', 'минут')} назад"


def expected_occupancy(model) -> dict[str, str]:
    """Учётная занятость путей по журналу операций: track_id -> номер поезда."""
    occ = {}
    if model is None:
        return occ
    for t in model.trains.values():
        if t.current_track_id and t.status in ("on_station",):
            occ[t.current_track_id] = t.number
    for o in model.ops.values():
        if o.status == "in_progress" and o.kind in ("arrival", "shunting", "departure"):
            tr = model.trains.get(o.train_id)
            if tr:
                occ.setdefault(o.track_id or o.from_track_id, tr.number)
                if o.from_track_id:
                    occ.setdefault(o.from_track_id, tr.number)
    return occ


def track_data_states(db: Session, now_real: datetime, model=None) -> dict[str, dict]:
    devices = [d for d in db.execute(select(Device).where(Device.kind == "track_circuit")).scalars()]
    obs = {(o.object_id, o.device_id): o for o in db.execute(
        select(Observation).where(Observation.attribute == "occupancy")).scalars()}
    overrides = {}
    for ov in db.execute(select(ManualOverride).where(ManualOverride.active.is_(True))).scalars():
        if aware(ov.valid_until) > now_real and ov.attribute == "occupancy":
            overrides[ov.object_id] = ov
    expected = expected_occupancy(model)
    labels = {}
    if model is not None:
        labels = {tid: model.track_label(tid) for tid in model.track_rows}
    out: dict[str, dict] = {}
    mono = time.monotonic()
    for d in devices:
        tid = d.object_id
        lab = labels.get(tid, tid)
        o = obs.get((tid, d.id))
        info = {"device_id": d.id, "device_name": d.name, "stale_after_s": d.stale_after_s}
        ov = overrides.get(tid)
        if ov is not None:
            out[tid] = {**info, "state": "actual", "source": "override", "observed": "occupied" if ov.value.get("occupied") else "free",
                        "age_s": 0, "message": f"Ручное уточнение: «{'занят' if ov.value.get('occupied') else 'свободен'}» "
                                               f"до {local_hm(ov.valid_until)} (основание: {ov.reason})",
                        "override_id": ov.id}
            continue
        if o is None:
            out[tid] = {**info, "state": "missing", "observed": None, "age_s": None,
                        "message": f"Нет данных занятости: {lab[:1].lower() + lab[1:]}. Подтверждение приёма на этот путь недоступно."}
            continue
        age = max(0.0, (now_real - aware(o.observed_at)).total_seconds())
        observed = "occupied" if o.value.get("occupied") else "free"
        base = {**info, "observed": observed, "age_s": round(age, 1), "observed_at": aware(o.observed_at).isoformat(),
                "received_at": aware(o.received_at).isoformat(),
                "latency_ms": round((aware(o.received_at) - aware(o.observed_at)).total_seconds() * 1000, 1)}
        rej_at = (d.health or {}).get("last_reject_at")
        if rej_at and datetime.fromisoformat(rej_at) > aware(o.received_at) and \
                (now_real - datetime.fromisoformat(rej_at)).total_seconds() < d.stale_after_s * 3:
            out[tid] = {**base, "state": "invalid",
                        "message": f"Последнее сообщение датчика ({lab[:1].lower() + lab[1:]}) отклонено: "
                                   f"{d.health.get('last_reject_reason', 'ошибка формата')}. Подтверждение приёма недоступно."}
            continue
        if age > d.stale_after_s:
            out[tid] = {**base, "state": "stale",
                        "message": f"Данные занятости: {lab[:1].lower() + lab[1:]} устарели. Последнее измерение получено {ago(age)}. "
                                   f"Подтверждение приёма на этот путь недоступно."}
            continue
        if o.quality == "bad":
            out[tid] = {**base, "state": "invalid",
                        "message": f"Датчик ({lab[:1].lower() + lab[1:]}) сообщает о плохом качестве измерения. Подтверждение приёма недоступно."}
            continue
        exp_train = expected.get(tid)
        mismatch = (observed == "free" and exp_train is not None) or (observed == "occupied" and exp_train is None)
        with _lock:
            if mismatch:
                since = _mismatch_since.setdefault(tid, mono)
            else:
                _mismatch_since.pop(tid, None)
                since = None
        if since is not None and mono - since >= CONTRADICTION_GRACE_S:
            if observed == "free":
                msg = (f"Противоречие: датчик сообщает «{lab} свободен», а по журналу операций там поезд № {exp_train}. "
                       f"Сохранены оба наблюдения; подтверждение операций на путь недоступно до уточнения.")
            else:
                msg = (f"Противоречие: датчик сообщает «{lab} занят», а по журналу операций путь свободен. "
                       f"Возможен посторонний подвижной состав. Подтверждение операций недоступно до уточнения.")
            out[tid] = {**base, "state": "contradictory", "expected": "occupied" if exp_train else "free",
                        "expected_train": exp_train, "message": msg,
                        "mismatch_s": round(mono - since, 1)}
            continue
        out[tid] = {**base, "state": "actual", "message": "Данные актуальны."}
    return out


def device_statuses(db: Session, now_real: datetime) -> list[dict]:
    """Состояние подключения (heartbeat) и качество последнего измерения — раздельно."""
    out = []
    for d in db.execute(select(Device).order_by(Device.kind, Device.id)).scalars():
        hb = aware(d.last_heartbeat_at)
        seen = aware(d.last_seen_at)
        hb_age = (now_real - hb).total_seconds() if hb else None
        hb_limit = max(15.0, d.period_s * 3)
        if hb_age is None:
            conn = "unknown"
        elif hb_age > hb_limit:
            conn = "offline"
        else:
            conn = "online"
        last_obs = (d.health or {}).get("last_observed_at")
        meas_age = (now_real - datetime.fromisoformat(last_obs)).total_seconds() if last_obs else None
        if meas_age is None:
            q = "missing"
        elif meas_age > d.stale_after_s:
            q = "stale"
        else:
            q = "actual"
        rej = (d.health or {}).get("last_reject_at")
        if rej and last_obs and datetime.fromisoformat(rej) > datetime.fromisoformat(last_obs) and \
                (now_real - datetime.fromisoformat(rej)).total_seconds() < d.stale_after_s * 3:
            q = "invalid"
        out.append({"id": d.id, "name": d.name, "kind": d.kind, "object_id": d.object_id,
                    "source_mode": d.source_mode, "status": d.status, "connection": conn,
                    "heartbeat_age_s": round(hb_age, 1) if hb_age is not None else None,
                    "last_seen_at": seen.isoformat() if seen else None, "data_quality": q,
                    "measurement_age_s": round(meas_age, 1) if meas_age is not None else None,
                    "stale_after_s": d.stale_after_s, "period_s": d.period_s, "x": d.x, "y": d.y,
                    "health": {k: v for k, v in (d.health or {}).items() if k in
                               ("battery", "rssi", "uptime", "last_reject_code", "last_reject_reason")}})
    return out
