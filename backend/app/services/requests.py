"""Жизненный цикл заявки между станциями.

Подтверждение — единственная операция, создающая резервы по заявке. Оно выполняется под
транзакционной блокировкой станции, повторно проверяет все ограничения на свежем состоянии
и сохраняет резервы с защитой ограничением-исключением БД. Поэтому конфликтную заявку нельзя
подтвердить ни из интерфейса, ни прямым API-запросом, ни двумя одновременными запросами.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit, domain_event
from app.core.errors import AppError, Conflict, NotFound
from app.core.timeutil import aware, iso, local_hm, utcnow
from app.domain.statuses import LABELS, transition
from app.models import Operation, Station, Train, TransferRequest, User, Wagon
from app.services.checker import check_request, train_length_for_request
from app.services.model import StationModel
from app.services.reservations import create_from_ops, release_for_train
from app.services.versioning import bump, station_lock

CHECK_TTL_VERSION_NOTE = "Проверка привязана к версии состояния; при изменении состояния требуется повторная проверка."


def _get(db: Session, rid: str, lock: bool = False) -> TransferRequest:
    q = select(TransferRequest).where(TransferRequest.id == rid)
    if lock:
        q = q.with_for_update()
    r = db.execute(q).scalar_one_or_none()
    if not r:
        raise NotFound("REQUEST_NOT_FOUND", "Заявка не найдена.")
    return r


def serialize(r: TransferRequest, model: StationModel | None = None) -> dict:
    stale = None
    if model is not None and r.last_check_version is not None:
        stale = r.last_check_version != model.version
    return {"id": r.id, "number": r.number, "from_station_id": r.from_station_id, "to_station_id": r.to_station_id,
            "wagons_count": r.wagons_count, "wagon_kind": r.wagon_kind, "train_length_m": r.train_length_m,
            "cargo": r.cargo, "priority": r.priority, "split_allowed": r.split_allowed,
            "desired_departure": iso(r.desired_departure), "status": r.status,
            "status_label": LABELS["request"].get(r.status, r.status), "last_check": r.last_check,
            "last_check_at": iso(r.last_check_at), "last_check_version": r.last_check_version,
            "check_stale": stale, "train_id": r.train_id, "parent_id": r.parent_id, "created_by": r.created_by,
            "created_at": iso(r.created_at), "updated_at": iso(r.updated_at), "decision_reason": r.decision_reason,
            "row_version": r.row_version}


def next_number(db: Session) -> str:
    n = db.execute(select(func.count(TransferRequest.id))).scalar_one() + 1
    while db.execute(select(TransferRequest).where(TransferRequest.number == f"Z-{n:04d}")).first():
        n += 1
    return f"Z-{n:04d}"


def create_request(db: Session, user: User, data: dict) -> TransferRequest:
    main = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
    src = db.get(Station, data["from_station_id"])
    if not src or src.kind != "neighbor":
        raise AppError("UNKNOWN_STATION", "Станция отправления должна быть соседней станцией модели.",
                       hint="Выберите станцию из списка соседних.")
    if data.get("wagon_kind") and data["wagon_kind"] not in main.config["processing"]["wagon_lengths_m"]:
        raise AppError("UNKNOWN_WAGON_KIND", f"Неизвестный род вагонов: {data['wagon_kind']}.")
    now = utcnow()
    r = TransferRequest(id=f"RQ-{uuid.uuid4().hex[:8]}", number=next_number(db), from_station_id=src.id,
                        to_station_id=main.id, wagons_count=data["wagons_count"], wagon_kind=data.get("wagon_kind"),
                        train_length_m=data.get("train_length_m"), cargo=data.get("cargo"),
                        priority=data.get("priority", 2), split_allowed=data.get("split_allowed", False),
                        desired_departure=data["desired_departure"], status="new", created_by=user.id,
                        created_at=now, updated_at=now)
    db.add(r)
    db.flush()
    sim_now = StationModel(db, with_reservations=False, data_states={}).now
    audit(db, user, "request.create", "transfer_request", r.id,
          f"Создана заявка {r.number}: {src.name} → {main.name}, {r.wagons_count} ваг., отправление {local_hm(r.desired_departure)}",
          after=serialize(r), model_time=sim_now)
    bump(db, "request:create")
    return r


def run_check(db: Session, user: User, rid: str) -> dict:
    r = _get(db, rid, lock=True)
    if r.status not in ("new", "checked", "confirmed"):
        raise Conflict("REQUEST_NOT_CHECKABLE", f"Заявку в статусе «{LABELS['request'][r.status]}» проверять не нужно.")
    model = StationModel(db)
    result = check_request(model, r)
    r.last_check = result
    r.last_check_at = utcnow()
    r.last_check_version = model.version
    if r.status == "new":
        transition("request", r, "checked")
    r.updated_at = utcnow()
    r.row_version += 1
    audit(db, user, "request.check", "transfer_request", r.id, f"Проверка заявки {r.number}: {result['decision_label']}. {result['summary']}",
          after={"decision": result["decision"], "state_version": model.version}, model_time=model.now)
    domain_event(db, "request.checked", f"Заявка {r.number}: {result['summary']}",
                 severity="info" if result["decision"].startswith("available") else "warning",
                 payload={"request_id": r.id, "decision": result["decision"]}, model_time=model.now)
    return {"request": serialize(r, model), "check": result}


def confirm(db: Session, user: User, rid: str, acknowledge_warnings: bool = False) -> dict:
    main = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
    station_lock(db, main.id)
    r = _get(db, rid, lock=True)
    if r.status != "checked":
        if r.status == "new":
            raise Conflict("CHECK_REQUIRED", "Перед подтверждением выполните проверку приёма.",
                           hint="Нажмите «Проверить приём».")
        raise Conflict("INVALID_STATUS_TRANSITION",
                       f"Заявку в статусе «{LABELS['request'][r.status]}» подтвердить нельзя.")
    model = StationModel(db)
    result = check_request(model, r, with_alternatives=True)  # повторная проверка на свежем состоянии
    r.last_check, r.last_check_at, r.last_check_version = result, utcnow(), model.version
    if result["decision"] not in ("available", "available_with_warnings"):
        raise Conflict("REQUEST_NOT_AVAILABLE", result["summary"],
                       details={"check": result},
                       hint="Выберите одну из проверенных альтернатив или измените параметры заявки.")
    warnings = [i for i in result["items"] if i["status"] == "warning"]
    if warnings and not acknowledge_warnings:
        raise Conflict("WARNINGS_NOT_ACKNOWLEDGED", "Есть предупреждения, требующие подтверждения: " +
                       "; ".join(w["message"] for w in warnings),
                       details={"check": result, "warnings": warnings},
                       hint="Ознакомьтесь с последствиями и подтвердите ещё раз с отметкой «С предупреждениями ознакомлен».")
    train = _create_train(db, model, r, result)
    transition("request", r, "confirmed")
    r.train_id = train.id
    r.updated_at = utcnow()
    r.row_version += 1
    r.decision_reason = "; ".join(w["message"] for w in warnings) if warnings else None
    ops = result["assignment"]["ops"]
    audit(db, user, "request.confirm", "transfer_request", r.id,
          f"Заявка {r.number} подтверждена: прибытие {local_hm(datetime.fromisoformat(ops[0]['start']))}, "
          f"{model.track_label_lc(ops[0]['track_id'])}; ресурсы зарезервированы" +
          (" (с предупреждениями)" if warnings else ""),
          after={"train_id": train.id, "assignment": result["assignment"], "warnings": [w["code"] for w in warnings]},
          model_time=model.now)
    domain_event(db, "request.confirmed", f"Заявка {r.number} подтверждена, поезд № {train.number}",
                 payload={"request_id": r.id, "train_id": train.id}, model_time=model.now)
    bump(db, "request:confirm")
    return {"request": serialize(r, model), "check": result, "train_id": train.id, "train_number": train.number}


def _create_train(db: Session, model: StationModel, r: TransferRequest, result: dict) -> Train:
    cnt = db.execute(select(func.count(Train.id)).where(Train.kind == "transfer")).scalar_one()
    number = str(3401 + cnt * 2)
    while db.get(Train, f"TR-{number}"):
        number = str(int(number) + 2)
    ops_spec = result["assignment"]["ops"]
    arr = datetime.fromisoformat(ops_spec[0]["start"])
    origin = model.neighbors.get(r.from_station_id)
    side = origin.config.get("side", "west") if origin else "west"
    length, _ = train_length_for_request(model, r)
    t = Train(id=f"TR-{number}", number=number, kind="transfer", priority=r.priority, origin_station_id=r.from_station_id,
              destination_station_id=model.sid, arrival_side=side, departure_side="east" if side == "west" else "west",
              wagons_count=r.wagons_count, length_m=length if not r.wagon_kind else None, loco_length_m=34.0,
              status="scheduled", scheduled_arrival=arr, expected_arrival=arr, transfer_request_id=r.id,
              cargo=r.cargo)
    db.add(t)
    db.flush()
    wl = model.cfg["processing"]["wagon_lengths_m"].get(r.wagon_kind) if r.wagon_kind else None
    base = 61000000 + abs(hash(r.id)) % 900000
    for i in range(r.wagons_count):
        db.add(Wagon(id=f"W{base + i}-{r.number}", number=str(base + i), kind=r.wagon_kind or "gondola", length_m=wl,
                     loaded=True, condition="ok", position=i + 1, train_id=t.id))
    ops = []
    for o in ops_spec:
        s, e = datetime.fromisoformat(o["start"]), datetime.fromisoformat(o["end"])
        ops.append(Operation(id=f"OP-{number}-{o['seq']}", station_id=model.sid, train_id=t.id, kind=o["kind"], seq=o["seq"],
                             track_id=o["track_id"], from_track_id=o.get("from_track_id"), side=o.get("side"),
                             duration_min=o["duration_min"], requirements=o["requirements"], resource_ids=o["resource_ids"],
                             route_nodes=o["route_nodes"], planned_start=s, planned_end=e, forecast_start=s,
                             forecast_end=e, not_before=arr if o["kind"] == "arrival" else None, status="confirmed",
                             reserved=True))
    db.add_all(ops)
    db.flush()
    model.wagons_by_train[t.id] = []
    create_from_ops(db, model, t.id, t.number, ops, request_id=r.id)
    return t


def cancel(db: Session, user: User, rid: str, reason: str) -> dict:
    main = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
    station_lock(db, main.id)
    r = _get(db, rid, lock=True)
    prev = r.status
    transition("request", r, "cancelled")
    released = 0
    if prev == "confirmed" and r.train_id:
        train = db.get(Train, r.train_id)
        ops = list(db.execute(select(Operation).where(Operation.train_id == train.id)).scalars())
        if any(o.status in ("in_progress", "done") for o in ops):
            raise Conflict("REQUEST_IN_EXECUTION", "Заявка уже исполняется — отменить её нельзя.")
        from app.models import Reservation
        released = db.execute(select(func.count(Reservation.id)).where(Reservation.train_id == train.id,
                                                                     Reservation.status == "confirmed")).scalar_one()
        release_for_train(db, train.id)
        for o in ops:
            transition("operation", o, "cancelled")
        transition("train", train, "cancelled")
    r.decision_reason = reason
    r.updated_at = utcnow()
    r.row_version += 1
    sim_now = StationModel(db, with_reservations=False, data_states={}).now
    audit(db, user, "request.cancel", "transfer_request", r.id,
          f"Заявка {r.number} отменена" + (f", освобождено резервов: {released}" if released else ""), reason=reason,
          before={"status": prev}, after={"status": "cancelled", "released_reservations": released}, model_time=sim_now)
    domain_event(db, "request.cancelled", f"Заявка {r.number} отменена. Причина: {reason}", payload={"request_id": r.id},
                 model_time=sim_now)
    bump(db, "request:cancel")
    return {"request": serialize(r), "released_reservations": released}


def reject(db: Session, user: User, rid: str, reason: str) -> dict:
    r = _get(db, rid, lock=True)
    transition("request", r, "rejected")
    r.decision_reason = reason
    r.updated_at = utcnow()
    r.row_version += 1
    sim_now = StationModel(db, with_reservations=False, data_states={}).now
    audit(db, user, "request.reject", "transfer_request", r.id, f"Отказ по заявке {r.number}", reason=reason, model_time=sim_now)
    domain_event(db, "request.rejected", f"Отказ по заявке {r.number}: {reason}", severity="warning",
                 payload={"request_id": r.id}, model_time=sim_now)
    bump(db, "request:reject")
    return {"request": serialize(r)}


def reschedule(db: Session, user: User, rid: str, departure: datetime) -> dict:
    """Перенос отправления. Для подтверждённой заявки — перепроверка и перерезервирование
    в одной транзакции: при отказе старые резервы сохраняются."""
    main = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
    station_lock(db, main.id)
    r = _get(db, rid, lock=True)
    if r.status not in ("new", "checked", "confirmed"):
        raise Conflict("INVALID_STATUS_TRANSITION", f"Заявку в статусе «{LABELS['request'][r.status]}» перенести нельзя.")
    model = StationModel(db)
    departure = aware(departure)
    if departure < model.now:
        raise AppError("TIME_IN_PAST", "Новое время отправления уже прошло.")
    old = r.desired_departure
    if r.status != "confirmed":
        r.desired_departure = departure
        result = check_request(model, r)
        r.last_check, r.last_check_at, r.last_check_version = result, utcnow(), model.version
        if r.status == "new":
            transition("request", r, "checked")
    else:
        train = db.get(Train, r.train_id)
        r.desired_departure = departure
        result = check_request(model, r, with_alternatives=False)
        if result["decision"] not in ("available", "available_with_warnings"):
            raise Conflict("REQUEST_NOT_AVAILABLE", "Перенос невозможен: " + result["summary"] + " Прежние резервы сохранены.",
                           details={"check": result})
        ops = list(db.execute(select(Operation).where(Operation.train_id == train.id).order_by(Operation.seq)).scalars())
        release_for_train(db, train.id)
        db.flush()
        for o, spec in zip(ops, result["assignment"]["ops"]):
            o.planned_start, o.planned_end = datetime.fromisoformat(spec["start"]), datetime.fromisoformat(spec["end"])
            o.track_id, o.from_track_id = spec["track_id"], spec.get("from_track_id")
            o.resource_ids, o.route_nodes = spec["resource_ids"], spec["route_nodes"]
            if o.kind == "arrival":
                o.not_before = o.planned_start
        arr = datetime.fromisoformat(result["assignment"]["ops"][0]["start"])
        train.scheduled_arrival = train.expected_arrival = arr
        db.flush()
        create_from_ops(db, model, train.id, train.number, ops, request_id=r.id)
        r.last_check, r.last_check_at, r.last_check_version = result, utcnow(), model.version
    r.updated_at = utcnow()
    r.row_version += 1
    audit(db, user, "request.reschedule", "transfer_request", r.id,
          f"Перенос отправления по заявке {r.number}: {local_hm(old)} → {local_hm(departure)}",
          before={"departure": iso(old)}, after={"departure": iso(departure), "decision": result["decision"]},
          model_time=model.now)
    bump(db, "request:reschedule")
    return {"request": serialize(r, model), "check": result}


def apply_alternative(db: Session, user: User, rid: str, action: dict) -> dict:
    t = action.get("type")
    if t == "postpone":
        return reschedule(db, user, rid, datetime.fromisoformat(action["departure"].replace("Z", "+00:00")))
    if t == "other_station":
        r = _get(db, rid, lock=True)
        st = db.get(Station, action["station_id"])
        if not st:
            raise NotFound("STATION_NOT_FOUND", "Станция не найдена.")
        model = StationModel(db)
        res = check_request(model, r)
        alt = next((a for a in res["alternatives"] if a["type"] == "other_station" and a["action"]["station_id"] == st.id), None)
        if not alt:
            raise Conflict("ALTERNATIVE_NOT_VALID", f"Направление на станцию {st.name} больше не проходит проверку.",
                           hint="Выполните проверку заново.")
        return cancel(db, user, rid, f"Перенаправлена на станцию {st.name} (проверено по упрощённой модели соседа: "
                                     f"{alt['description']})")
    if t == "split":
        r = _get(db, rid, lock=True)
        if not r.split_allowed:
            raise Conflict("SPLIT_NOT_ALLOWED", "Разделение партии не разрешено заявкой.")
        children = []
        for i, part in enumerate(action["parts"], start=1):
            dep = datetime.fromisoformat(part["departure"].replace("Z", "+00:00"))
            c = create_request(db, user, {"from_station_id": r.from_station_id, "wagons_count": part["wagons"],
                                          "wagon_kind": r.wagon_kind, "cargo": r.cargo, "priority": r.priority,
                                          "desired_departure": dep, "split_allowed": False})
            c.parent_id = r.id
            children.append(c)
        db.flush()
        cancel(db, user, rid, "Разделена на заявки " + ", ".join(c.number for c in children))
        return {"children": [serialize(c) for c in children]}
    if t == "reorder":
        from app.services.plans import apply_train_moves
        res = apply_train_moves(db, user, action["train_id"], action["ops"],
                                reason=f"Изменение очереди для приёма по заявке {_get(db, rid).number}")
        return res
    raise AppError("UNKNOWN_ALTERNATIVE", "Неизвестный вид альтернативы.")


def expire_overdue(db: Session, now: datetime) -> int:
    n = 0
    for r in db.execute(select(TransferRequest).where(TransferRequest.status.in_(["new", "checked"]))).scalars():
        if aware(r.desired_departure) < now - timedelta(minutes=30):
            transition("request", r, "expired")
            r.updated_at = utcnow()
            domain_event(db, "request.expired", f"Заявка {r.number} просрочена: время отправления прошло без подтверждения",
                         severity="warning", payload={"request_id": r.id}, model_time=now)
            n += 1
    return n
