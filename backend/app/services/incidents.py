"""Инциденты: регистрация, последствия в модели и устранение.

Инцидент не «рисует» последствия сам: он меняет исходные данные (закрытие интервала,
дополнительная длительность, новое требование к операции), а конфликты и задержки затем
вычисляются детектором и планировщиком.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit, domain_event
from app.core.errors import AppError, NotFound
from app.core.timeutil import aware, local_hm, utcnow
from app.domain.statuses import transition
from app.models import Device, Incident, Operation, Resource, SimState, Station, Track, Train, User, Wagon
from app.services.versioning import bump

KIND_LABELS = {
    "track_closure": "Закрытие пути",
    "switch_failure": "Неисправность стрелки",
    "cargo_delay": "Задержка грузовой операции",
    "faulty_wagon": "Неисправный вагон",
    "neighbor_restriction": "Ограничение приёма соседней станцией",
    "resource_failure": "Отказ ресурса",
}


def _id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _now(db) -> datetime:
    return aware(db.get(SimState, 1).model_time)


def create_incident(db: Session, user: User | None, kind: str, object_id: str | None, *,
                    duration_min: int | None = None, extra_min: int | None = None,
                    title: str | None = None, description: str = "", start_at: datetime | None = None,
                    params: dict | None = None) -> Incident:
    if kind not in KIND_LABELS:
        raise AppError("UNKNOWN_INCIDENT_KIND", f"Неизвестный вид инцидента: {kind}")
    now = _now(db)
    start = aware(start_at) or now
    end = start + timedelta(minutes=duration_min) if duration_min else None
    params = dict(params or {})
    station = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
    obj_type, label = None, object_id
    if kind in ("track_closure",):
        t = db.get(Track, object_id)
        if not t:
            raise NotFound("TRACK_NOT_FOUND", f"Путь {object_id} не найден.")
        obj_type, label = "track", (f"Главный путь {t.number}" if t.kind == "main" else f"Путь {t.number}")
    elif kind == "switch_failure":
        obj_type, label = "switch", object_id
    elif kind == "resource_failure":
        r = db.get(Resource, object_id)
        if not r:
            raise NotFound("RESOURCE_NOT_FOUND", f"Ресурс {object_id} не найден.")
        obj_type, label = "resource", r.name
    elif kind == "neighbor_restriction":
        n = db.get(Station, object_id)
        if not n or n.kind != "neighbor":
            raise NotFound("STATION_NOT_FOUND", f"Соседняя станция {object_id} не найдена.")
        obj_type, label = "station", n.name
    elif kind == "cargo_delay":
        op = _cargo_op(db, object_id)
        if not op:
            raise AppError("NO_CARGO_OPERATION", "Не найдена текущая или ближайшая грузовая операция для задержки.",
                           hint="Укажите грузовой путь, на котором выполняется погрузка или выгрузка.")
        extra = int(extra_min or 45)
        op.extra_delay_min = (op.extra_delay_min or 0) + extra
        params.update({"operation_id": op.id, "extra_min": extra})
        obj_type, object_id = "operation", op.id
        tr = db.get(Train, op.train_id) if op.train_id else None
        label = f"{'выгрузка' if op.kind == 'unloading' else 'погрузка'} поезда № {tr.number if tr else '—'}"
    elif kind == "faulty_wagon":
        w = db.get(Wagon, object_id)
        if not w:
            raise NotFound("WAGON_NOT_FOUND", f"Вагон {object_id} не найден.")
        if user is not None:
            # действие пользователя: сообщение → решение → работы → контрольный осмотр (без автоприёмки)
            return report_faulty_wagon_by_user(db, user, w, description or "", now)
        return register_faulty_wagon(db, w, source="система", model_time=now, user=None)

    inc = Incident(id=_id("INC"), station_id=station.id, kind=kind,
                   title=title or f"{KIND_LABELS[kind]}: {label}",
                   description=description or "", object_type=obj_type, object_id=object_id,
                   start_at=start, end_at=end, status="active", severity="high", params=params,
                   created_by=user.id if user else None, created_at=utcnow())
    db.add(inc)
    period = f"с {local_hm(start)}" + (f" до {local_hm(end)}" if end else " до отмены")
    audit(db, user, "incident.create", "incident", inc.id, f"Зарегистрирован инцидент «{inc.title}» {period}",
          after={"kind": kind, "object_id": object_id, "start": start.isoformat(),
                 "end": end.isoformat() if end else None, **params}, model_time=now)
    domain_event(db, "incident.created", f"{inc.title} ({period})", severity="warning",
                 payload={"incident_id": inc.id, "kind": kind, "object_id": object_id}, model_time=now)
    bump(db, f"incident:{kind}")
    return inc


def _cargo_op(db: Session, object_id: str | None) -> Operation | None:
    q = select(Operation).where(Operation.kind.in_(["loading", "unloading"]),
                                Operation.status.in_(["in_progress", "planned", "confirmed"]))
    if object_id:
        op = db.get(Operation, object_id)
        if op and op.kind in ("loading", "unloading"):
            return op
        q = q.where(Operation.track_id == object_id)
    ops = sorted(db.execute(q).scalars(), key=lambda o: (o.status != "in_progress", o.planned_start))
    return ops[0] if ops else None


def report_faulty_wagon_by_user(db: Session, user: User, w: Wagon, details: str, now: datetime) -> Incident:
    """Диспетчер сообщает о неисправном вагоне из кабинета. Создаётся сообщение о дефекте с
    временным ограничением (вагон не может уйти со станцией до решения) и инцидент-запись в журнале.
    Отцепка и ремонт назначаются решением по сообщению; устранение подтверждает контрольный осмотр."""
    from app.models import DefectReport
    from app.services import workflow as wf
    station = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
    train = db.get(Train, w.train_id) if w.train_id else None
    num = (db.execute(select(func.coalesce(func.max(DefectReport.number), 0))).scalar() or 0) + 1
    t = utcnow()
    r = DefectReport(id=wf._uid("DR"), number=num, client_uuid=f"desk-{uuid.uuid4().hex}", content_hash="desk",
                     station_id=station.id, author_id=user.id, wagon_id=w.id, wagon_number_raw=w.number,
                     train_id=train.id if train else None, track_id=train.current_track_id if train else w.track_id,
                     position=w.position if train else None, category="other", component=None,
                     description=details or "Неисправность зарегистрирована диспетчером в кабинете",
                     urgency="urgent", status="under_review", restriction_active=True, created_at=t, updated_at=t,
                     acknowledged_at=t, acknowledged_by=user.id, version=1)
    db.add(r)
    inc = Incident(id=_id("INC"), station_id=station.id, kind="faulty_wagon",
                   title=f"Неисправный вагон № {w.number}" + (f" в составе поезда № {train.number}" if train else ""),
                   description=f"Источник: {user.full_name}. Сообщение № {num}: решение — в разделе «Сообщения работников».",
                   object_type="wagon", object_id=w.id, start_at=now, end_at=None, status="active", severity="high",
                   params={"train_id": train.id if train else None, "source": user.full_name, "defect_report_id": r.id,
                           "mode": "workflow"}, created_by=user.id, created_at=utcnow())
    db.add(inc)
    wf._event(db, "defect", r.id, user, "status", None, "under_review", "Зарегистрировано диспетчером; ограничение до решения")
    db.flush()
    wf.recompute_wagon(db, w)
    audit(db, user, "incident.create", "incident", inc.id, inc.title, after={"wagon": w.number, "defect_report": r.id},
          model_time=now)
    domain_event(db, "incident.created", f"{inc.title}. Требуется решение по сообщению № {num}.", severity="warning",
                 payload={"incident_id": inc.id, "kind": "faulty_wagon", "defect_report_id": r.id}, model_time=now)
    wf._changed(db, "defect", r.id, station.id)
    bump(db, "incident:faulty_wagon")
    return inc


def register_faulty_wagon(db: Session, w: Wagon, *, source: str, model_time: datetime | None,
                          details: str = "", user: User | None = None) -> Incident:
    """Сценарный режим (демо-сценарии, диагностический комплекс): требуется отцепка и ремонт до
    отправления; ремонт демо-инцидента завершает движок. Ручные работы работников так не принимаются.

    Новые операции создаются как «требования без резерва» (reserved=False); их размещение во
    времени и ресурсах выполняет планировщик, а до этого детектор показывает конфликт."""
    now = model_time or _now(db)
    station = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
    cfg = station.config
    w.condition = "faulty"
    train = db.get(Train, w.train_id) if w.train_id else None
    depot = db.execute(select(Track).where(Track.kind == "repair")).scalars().first()
    inc = Incident(id=_id("INC"), station_id=station.id, kind="faulty_wagon",
                   title=f"Неисправный вагон № {w.number}" + (f" в составе поезда № {train.number}" if train else ""),
                   description=f"Источник: {source}. {details}".strip(), object_type="wagon", object_id=w.id,
                   start_at=now, end_at=None, status="active", severity="high",
                   params={"train_id": train.id if train else None, "source": source, "mode": "scenario"},
                   created_by=user.id if user else None, created_at=utcnow())
    db.add(inc)
    if train and depot:
        ops = list(db.execute(select(Operation).where(Operation.train_id == train.id).order_by(Operation.seq)).scalars())
        dep = next((o for o in ops if o.kind == "departure" and o.status in ("planned", "confirmed")), None)
        if dep:
            prev = [o for o in ops if o.seq < dep.seq]
            cur_track = dep.track_id
            start = max(now + timedelta(minutes=5), max((aware(o.planned_end) for o in prev), default=now))
            unc = Operation(id=_id("OP"), station_id=station.id, train_id=train.id, kind="uncoupling",
                            seq=dep.seq, track_id=depot.id, from_track_id=cur_track, side="east",
                            duration_min=20, requirements=["shunting_loco", "loco_crew", "shunting_crew"],
                            resource_ids=[], route_nodes=[], planned_start=start,
                            planned_end=start + timedelta(minutes=20), status="planned", reserved=False,
                            note=f"Отцепка неисправного вагона № {w.number}")
            dep.seq += 1
            db.add(unc)
            rep = Operation(id=_id("OP"), station_id=station.id, train_id=None, kind="repair", seq=0,
                            track_id=depot.id, duration_min=120, requirements=["repair_team"], resource_ids=[],
                            route_nodes=[], planned_start=start + timedelta(minutes=20),
                            planned_end=start + timedelta(minutes=140), not_before=start + timedelta(minutes=20),
                            status="planned", reserved=False, note=f"Ремонт вагона № {w.number}")
            db.add(rep)
            inc.params = {**inc.params, "uncoupling_op": unc.id, "repair_op": rep.id}
    elif train and not depot:
        inc.description += " На станции нет ремонтной зоны: отправление состава с неисправным вагоном моделью не допускается, требуется решение диспетчера."
    sim = db.get(SimState, 1)
    if sim and (sim.world or {}).get("diag_faulty_next"):
        sim.world = {**sim.world, "diag_faulty_next": False}  # сценарная неисправность обнаружена
    audit(db, user, "incident.create", "incident", inc.id, inc.title, after={"wagon": w.number, "source": source},
          model_time=now)
    domain_event(db, "incident.created", f"{inc.title}. {inc.description}", severity="warning",
                 payload={"incident_id": inc.id, "kind": "faulty_wagon"}, model_time=now)
    bump(db, "incident:faulty_wagon")
    return inc


def equipment_state_changed(db: Session, dev: Device, state: str, model_time) -> bool:
    res = db.get(Resource, dev.object_id)
    if not res:
        return False
    active = db.execute(select(Incident).where(Incident.kind == "resource_failure", Incident.status == "active",
                                               Incident.object_id == res.id)).scalars().first()
    if state == "fault" and not active:
        station = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
        inc = Incident(id=_id("INC"), station_id=station.id, kind="resource_failure",
                       title=f"Отказ ресурса: {res.name}", description=f"По данным датчика «{dev.name}»",
                       object_type="resource", object_id=res.id, start_at=model_time, end_at=None,
                       status="active", severity="high", params={"source": "telemetry", "device_id": dev.id},
                       created_at=utcnow())
        db.add(inc)
        domain_event(db, "incident.created", inc.title, severity="warning",
                     payload={"incident_id": inc.id, "kind": "resource_failure"}, model_time=model_time)
        bump(db, "telemetry:equipment_fault")
        return True
    if state in ("working", "idle") and active and (active.params or {}).get("source") == "telemetry":
        transition("incident", active, "resolved")
        active.end_at = model_time
        domain_event(db, "incident.resolved", f"Ресурс «{res.name}» снова исправен (по данным датчика)",
                     payload={"incident_id": active.id}, model_time=model_time)
        bump(db, "telemetry:equipment_ok")
        return True
    return False


def resolve_incident(db: Session, user: User | None, incident_id: str, reason: str | None = None) -> Incident:
    inc = db.get(Incident, incident_id)
    if not inc:
        raise NotFound("INCIDENT_NOT_FOUND", "Инцидент не найден.")
    now = _now(db)
    if inc.kind == "faulty_wagon" and (inc.params or {}).get("defect_report_id"):
        from app.models import DefectReport
        r = db.get(DefectReport, inc.params["defect_report_id"])
        if r and (r.fault_open or r.restriction_active or r.status in ("submitted", "acknowledged", "under_review", "needs_info")):
            raise AppError("DEFECT_NOT_CLOSED", "Закрыть инцидент нельзя: неисправность по сообщению № "
                           f"{r.number} ещё не устранена и не принята контрольным осмотром.", status=409,
                           hint="Устранение подтверждает проверяющий по заявке на работы (раздел «Сообщения работников»).")
    transition("incident", inc, "resolved")
    inc.end_at = now if not inc.end_at or aware(inc.end_at) > now else inc.end_at
    if inc.kind == "faulty_wagon":
        w = db.get(Wagon, inc.object_id)
        if w:
            db.flush()
            from app.services.workflow import recompute_wagon
            recompute_wagon(db, w)  # состояние вагона — по всем его дефектам, не по одному инциденту
    audit(db, user, "incident.resolve", "incident", inc.id, f"Инцидент «{inc.title}» устранён", reason=reason,
          model_time=now)
    domain_event(db, "incident.resolved", f"Устранён: {inc.title}", payload={"incident_id": inc.id}, model_time=now)
    bump(db, "incident:resolved")
    return inc
