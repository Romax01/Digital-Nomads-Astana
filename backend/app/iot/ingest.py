"""Сервис приёма телеметрии: проверка, нормализация, сохранение, применение к наблюдаемому состоянию.

Порядок проверок: JSON → структура и версия схемы → устройство в реестре → станция →
разрешённый тип события → связанный объект → единицы и диапазоны → время → повтор →
порядок (sequence_number в рамках boot_id) → актуальность.

Правила, важные для бизнес-логики:
* повтор (тот же device_id + event_id) не выполняет повторного действия;
* событие, пришедшее не по порядку или опоздавшее, сохраняется в истории, но не заменяет
  более новое наблюдение;
* устаревшее (в т.ч. retained) сообщение не «освежает» данные: путь не становится «свободным»
  по старому измерению;
* смена boot_id — перезапуск устройства: счётчик последовательности начинается заново.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.audit import domain_event
from app.core.timeutil import aware, utcnow
from app.iot.contract import MAX_AGE_ACCEPT, MAX_FUTURE_SKEW, Reject, normalize_payload, validate_structure
from app.models import Device, Incident, Observation, SimState, TelemetryEvent, TelemetryReject, Wagon

ATTRIBUTE = {
    "occupancy": "occupancy", "rfid_read": "rfid", "position": "position", "switch_position": "switch",
    "equipment_state": "equipment", "diagnostics": "diagnostics", "heartbeat": "heartbeat",
}

# Значимые изменения обрабатываются раньше частых координатных обновлений.
PRIORITY = {"occupancy": 0, "switch_position": 0, "rfid_read": 0, "diagnostics": 0, "equipment_state": 1,
            "heartbeat": 1, "position": 2}


@dataclass
class RawItem:
    topic: str
    payload: bytes
    retained: bool = False
    received_at: datetime = field(default_factory=utcnow)
    received_mono: float = field(default_factory=time.monotonic)


@dataclass
class BatchResult:
    applied: int = 0
    rejected: int = 0
    duplicates: int = 0
    late: int = 0
    stale: int = 0
    touched_objects: set = field(default_factory=set)
    significant: bool = False
    earliest_received_mono: float | None = None
    business_events: list = field(default_factory=list)


class IngestProcessor:
    def __init__(self):
        self._seen: OrderedDict = OrderedDict()  # LRU последних event_id

    def _remember(self, key):
        self._seen[key] = True
        if len(self._seen) > 50000:
            self._seen.popitem(last=False)

    def reject(self, db: Session, item: RawItem, code: str, reason: str, device_id: str | None, res: BatchResult):
        raw = item.payload.decode("utf-8", errors="replace")[:4000]
        db.add(TelemetryReject(received_at=item.received_at, topic=item.topic[:160], device_id=device_id,
                               reason_code=code, reason=reason, raw=raw))
        metrics.incr(f"telemetry_rejected.{code}")
        res.rejected += 1
        if device_id:
            dev = db.get(Device, device_id)
            if dev:
                h = dict(dev.health or {})
                h["last_reject_at"] = item.received_at.isoformat()
                h["last_reject_code"] = code
                h["last_reject_reason"] = reason
                dev.health = h

    def process_batch(self, db: Session, items: list[RawItem]) -> BatchResult:
        res = BatchResult()
        devices = {d.id: d for d in db.execute(select(Device)).scalars()}
        parsed = []
        for it in items:
            if res.earliest_received_mono is None or it.received_mono < res.earliest_received_mono:
                res.earliest_received_mono = it.received_mono
            device_hint = None
            try:
                try:
                    d = json.loads(it.payload)
                except (ValueError, UnicodeDecodeError):
                    raise Reject("BAD_JSON", "Сообщение не является корректным JSON")
                device_hint = d.get("device_id") if isinstance(d, dict) else None
                m = validate_structure(d)
                parts = it.topic.split("/")
                if len(parts) != 4 or parts[0] != "station" or parts[1] != m.station_id or parts[3] != m.device_id:
                    raise Reject("TOPIC_MISMATCH", f"Топик {it.topic!r} не соответствует станции и устройству сообщения")
                dev = devices.get(m.device_id)
                if dev is None:
                    raise Reject("UNKNOWN_DEVICE", f"Устройство {m.device_id!r} отсутствует в реестре")
                if dev.status != "active":
                    raise Reject("DEVICE_DISABLED", f"Устройство {m.device_id} отключено в реестре")
                if dev.station_id != m.station_id:
                    raise Reject("WRONG_STATION", f"Устройство зарегистрировано на станции {dev.station_id}, "
                                                  f"а сообщение относится к {m.station_id}")
                if m.event_type not in (dev.allowed_event_types or []):
                    raise Reject("EVENT_NOT_ALLOWED", f"Устройству {dev.id} не разрешён тип события {m.event_type}")
                if m.object_id != dev.object_id:
                    raise Reject("OBJECT_MISMATCH", f"Объект {m.object_id} не связан с устройством {dev.id} "
                                                    f"(ожидается {dev.object_id})")
                norm = normalize_payload(m.event_type, m.payload)
                if m.observed_at - it.received_at > MAX_FUTURE_SKEW:
                    raise Reject("TIME_IN_FUTURE", "Время измерения опережает время приёма более чем на 5 с — "
                                                   "часы устройства не синхронизированы")
                if it.received_at - m.observed_at > MAX_AGE_ACCEPT:
                    raise Reject("TOO_OLD", "Измерение старше 24 ч")
                parsed.append((it, m, norm, dev))
            except Reject as r:
                self.reject(db, it, r.code, r.reason, device_hint if isinstance(device_hint, str) else None, res)

        # повторы: LRU + проверка в БД одним запросом
        keys = [(m.device_id, m.event_id) for _, m, _, _ in parsed]
        existing = set()
        if keys:
            rows = db.execute(select(TelemetryEvent.device_id, TelemetryEvent.event_id).where(
                TelemetryEvent.event_id.in_([k[1] for k in keys]))).all()
            existing = {(a, b) for a, b in rows}
        batch_seen = set()
        sim = db.get(SimState, 1)
        for it, m, norm, dev in parsed:
            key = (m.device_id, m.event_id)
            if key in existing or key in batch_seen or key in self._seen:
                res.duplicates += 1
                metrics.incr("telemetry_duplicates")
                continue
            batch_seen.add(key)
            self._remember(key)
            disposition = "applied"
            # перезапуск устройства / порядок
            last_obs_s = (dev.health or {}).get("last_observed_at")
            last_obs = datetime.fromisoformat(last_obs_s) if last_obs_s else None
            if dev.last_boot_id and dev.last_boot_id != m.boot_id:
                if last_obs is None or m.observed_at >= last_obs:
                    domain_event(db, "device.rebooted",
                                 f"Устройство «{dev.name}» перезапущено: новый сеанс {m.boot_id}, счётчик сообщений сброшен.",
                                 severity="warning", payload={"device_id": dev.id, "boot_id": m.boot_id},
                                 model_time=aware(sim.model_time) if sim else None)
                    dev.last_boot_id = m.boot_id
                    dev.last_seq = m.sequence_number
                else:
                    disposition = "late"  # сообщение прежнего сеанса пришло после нового
            elif dev.last_boot_id == m.boot_id and dev.last_seq is not None and m.sequence_number <= dev.last_seq:
                disposition = "late"
            else:
                dev.last_boot_id = m.boot_id
                dev.last_seq = m.sequence_number
            age = (it.received_at - m.observed_at).total_seconds()
            if disposition == "applied" and (age > dev.stale_after_s or it.retained and age > dev.period_s * 2):
                disposition = "stale"
            dev.last_seen_at = it.received_at
            attr = ATTRIBUTE[m.event_type]
            db.add(TelemetryEvent(event_id=m.event_id, device_id=m.device_id, station_id=m.station_id,
                                  object_id=m.object_id, event_type=m.event_type, observed_at=m.observed_at,
                                  received_at=it.received_at, sequence_number=m.sequence_number,
                                  boot_id=m.boot_id, payload=norm, quality=m.quality,
                                  source_mode=m.source_mode, schema_version=m.schema_version,
                                  disposition=disposition))
            metrics.observe("telemetry_transport_ms", max(0.0, age * 1000))
            if disposition == "late":
                res.late += 1
                metrics.incr("telemetry_late")
            elif disposition == "stale":
                res.stale += 1
                metrics.incr("telemetry_stale")
            if disposition in ("late", "stale"):
                continue
            h = dict(dev.health or {})
            if last_obs is None or m.observed_at > last_obs:
                h["last_observed_at"] = m.observed_at.isoformat()
            if m.event_type == "heartbeat":
                dev.last_heartbeat_at = m.observed_at
                h.update({k: v for k, v in norm.items()})
                dev.health = h
                res.applied += 1
                continue
            dev.health = h
            obs = db.get(Observation, (m.object_id, attr, m.device_id))
            if obs is None:
                obs = Observation(object_id=m.object_id, attribute=attr, device_id=m.device_id, value=norm,
                                  observed_at=m.observed_at, received_at=it.received_at, quality=m.quality,
                                  event_id=m.event_id)
                db.add(obs)
                changed = True
            elif m.observed_at > aware(obs.observed_at):
                changed = obs.value != norm or obs.quality != m.quality
                obs.value, obs.observed_at, obs.received_at = norm, m.observed_at, it.received_at
                obs.quality, obs.event_id = m.quality, m.event_id
            else:
                res.late += 1
                continue
            res.applied += 1
            res.touched_objects.add(m.object_id)
            if changed and PRIORITY[m.event_type] == 0:
                res.significant = True
            self._business_rules(db, m, norm, dev, sim, res)
        return res

    def _business_rules(self, db: Session, m, norm, dev: Device, sim, res: BatchResult):
        """Доменные следствия телеметрии. Повторная доставка сюда не попадает (дедупликация выше)."""
        mt = aware(sim.model_time) if sim else None
        if m.event_type == "rfid_read":
            nums = norm["wagon_numbers"]
            if nums:
                for w in db.execute(select(Wagon).where(Wagon.number.in_(nums))).scalars():
                    w.last_checkpoint = m.object_id
                    w.last_seen_at = m.observed_at
        elif m.event_type == "diagnostics" and norm.get("verdict") == "faulty" and norm.get("wagon_number"):
            wn = norm["wagon_number"]
            w = db.execute(select(Wagon).where(Wagon.number == wn)).scalar_one_or_none()
            exists = db.execute(select(Incident).where(Incident.kind == "faulty_wagon", Incident.status == "active",
                                                       Incident.object_id == (w.id if w else wn))).first()
            if w and not exists and w.condition == "ok":
                from app.services.incidents import register_faulty_wagon
                register_faulty_wagon(db, w, source=f"датчик {dev.name}", model_time=mt,
                                      details=f"температура буксы {norm.get('axle_temp', '—')} °C")
                res.business_events.append(("faulty_wagon", w.id))
                res.significant = True
        elif m.event_type == "equipment_state":
            from app.services.incidents import equipment_state_changed
            if equipment_state_changed(db, dev, norm["state"], mt):
                res.significant = True
                res.business_events.append(("equipment", dev.object_id))


def classify_priority(payload: bytes) -> int:
    """Быстрая классификация без полного разбора для очереди с приоритетами."""
    if b'"event_type": "position"' in payload or b'"event_type":"position"' in payload:
        return 2
    return 0
