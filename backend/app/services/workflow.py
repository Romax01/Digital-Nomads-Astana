"""Процесс работников: сообщение о дефекте → решение → заявка на работы → выполнение → контрольный
осмотр. Демонстрационная система поддержки решений: действия не являются командами автоматике.

Разделены пять сущностей:
* сообщение о дефекте (DefectReport) — то, что увидел работник;
* подтверждённая неисправность — DefectReport.fault_open (после решения диспетчера);
* заявка на работы (WorkOrder) — что и кому делать;
* технологические операции станции (Operation.work_order_id) — отцепка или замена в составе,
  их размещает планировщик, выполняет движок в модельном времени;
* результат контрольного осмотра (WorkInspection) — принимает другой уполномоченный сотрудник.

Время людей — реальное (utcnow): ускорение симуляции не ускоряет сроки ответа работников.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import bus
from app.core.audit import audit, domain_event
from app.core.errors import AppError, Conflict, Forbidden, NotFound
from app.core.permissions import can, role_label, role_permissions
from app.core.timeutil import aware, iso, utcnow
from app.models import (
    Attachment, DefectReport, Incident, Notification, Operation, SimState, Station, Track, Train, User, UserScope,
    Wagon, WorkEvent, WorkInspection, WorkOrder,
)
from app.services.versioning import bump

CATEGORIES = {"wheelset": "Колёсная пара", "axlebox": "Буксовый узел", "brake": "Тормозное оборудование",
              "coupler": "Автосцепка", "body": "Кузов / рама", "other": "Другое"}
URGENCY = {"normal": "Обычная", "urgent": "Срочная", "critical": "Предположительно критическая"}
DEFECT_STATUS = {"submitted": "Доставлено, ожидает приёма", "acknowledged": "Получение подтверждено",
                 "under_review": "На рассмотрении", "needs_info": "Запрошено уточнение", "accepted": "Принято",
                 "rejected": "Отклонено", "duplicate": "Дубликат"}
WO_KIND = {"inspection": "Дополнительный осмотр", "repair_in_place": "Ремонт без отцепки",
           "uncoupling_repair": "Отцепка и ремонт", "replacement": "Замена вагона в составе",
           "transfer": "Передача на другую ремонтную площадку"}
WO_STATUS = {"created": "Создана", "assigned": "Назначена", "in_progress": "Выполняется", "on_hold": "Приостановлена",
             "awaiting_inspection": "Ожидает контрольного осмотра", "rework": "Возвращена на доработку",
             "completed": "Принята", "cancelled": "Отменена"}
WAGON_CONDITION = {"ok": "Исправен", "faulty": "Неисправен", "in_repair": "В ремонте (отцеплен)",
                   "restricted": "Ограничение до проверки сообщения"}
DEFECT_OPEN = ("submitted", "acknowledged", "under_review", "needs_info")

# Матрица переходов сообщения: (из, в) -> право
DEFECT_TRANSITIONS = {
    ("submitted", "acknowledged"): "defect.triage",
    ("submitted", "under_review"): "defect.triage",
    ("acknowledged", "under_review"): "defect.triage",
    ("acknowledged", "needs_info"): "defect.triage",
    ("under_review", "needs_info"): "defect.triage",
    ("submitted", "needs_info"): "defect.triage",
    ("needs_info", "under_review"): "defect.triage|author",  # ответ автора или решение принимающего
    ("acknowledged", "accepted"): "work_order.create",
    ("under_review", "accepted"): "work_order.create",
    **{(s, "rejected"): "defect.triage" for s in DEFECT_OPEN},
    **{(s, "duplicate"): "defect.triage" for s in DEFECT_OPEN},
}
# Матрица переходов заявки на работы: (из, в) -> право
WO_TRANSITIONS = {
    ("created", "assigned"): "work_order.assign",
    ("assigned", "assigned"): "work_order.assign",      # переназначение
    ("on_hold", "assigned"): "work_order.assign",
    ("rework", "assigned"): "work_order.assign",
    ("assigned", "in_progress"): "work_order.execute",
    ("rework", "in_progress"): "work_order.execute",
    ("assigned", "on_hold"): "work_order.execute",
    ("in_progress", "on_hold"): "work_order.execute",
    ("on_hold", "in_progress"): "work_order.execute",   # возобновление (из on_hold — в прежнее рабочее состояние)
    ("in_progress", "awaiting_inspection"): "work_order.execute",
    ("awaiting_inspection", "completed"): "work_order.inspect",
    ("awaiting_inspection", "rework"): "work_order.inspect",
    **{(s, "cancelled"): "work_order.create" for s in ("created", "assigned", "in_progress", "on_hold", "rework")},
}


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def main_station(db: Session) -> Station:
    st = db.execute(select(Station).where(Station.kind == "main")).scalar_one_or_none()
    if st is None:
        raise AppError("NO_STATION", "Станция не загружена.", status=503)
    return st


def scope_of(db: Session, user: User) -> UserScope:
    """Область доступа. Для ролей кабинета без явной привязки — основная станция."""
    sc = db.get(UserScope, user.id)
    if sc:
        return sc
    st = db.execute(select(Station).where(Station.kind == "main")).scalar_one_or_none()
    return UserScope(user_id=user.id, station_id=st.id if st else "—", pto_id=None, brigade_id=None)


def _model_now(db: Session) -> datetime | None:
    s = db.get(SimState, 1)
    return aware(s.model_time) if s else None


def _event(db, entity_type, entity_id, user: User | None, kind, frm=None, to=None, text=""):
    db.add(WorkEvent(entity_type=entity_type, entity_id=entity_id, user_id=user.id if user else None,
                     user_name=user.full_name if user else "Система", kind=kind, from_status=frm, to_status=to,
                     text=text or "", created_at=utcnow()))


def _changed(db: Session, entity_type: str, entity_id: str, station_id: str):
    """Изменение для подписчиков WebSocket (публикуется после фиксации транзакции)."""
    bus.publish_after_commit(db, "work_changed", {"entity_type": entity_type, "entity_id": entity_id,
                                                  "station_id": station_id})


def notify(db: Session, user_ids, kind: str, title: str, body: str, entity_type: str, entity_id: str,
           exclude: str | None = None):
    for uid in sorted(set(u for u in user_ids if u and u != exclude)):
        db.add(Notification(user_id=uid, kind=kind, title=title, body=body, entity_type=entity_type,
                            entity_id=entity_id, created_at=utcnow()))
        bus.publish_after_commit(db, "notification", {"user_id": uid})


def users_with(db: Session, action: str, station_id: str, *, pto_id: str | None = None) -> list[str]:
    out = []
    for u in db.execute(select(User).where(User.active.is_(True))).scalars():
        if action not in role_permissions(u.role):
            continue
        sc = scope_of(db, u)
        if sc.station_id != station_id:
            continue
        if pto_id and sc.pto_id and sc.pto_id != pto_id:
            continue
        out.append(u.id)
    return out


def _check_version(obj, expected: int | None):
    if expected is not None and obj.version != expected:
        raise Conflict("STALE_VERSION", "Запись уже изменена другим пользователем.",
                       details={"current_version": obj.version, "your_version": expected},
                       hint="Обновите карточку: действие выполняется только по актуальному состоянию.")


def _need(user: User, action: str, what: str):
    if not can(user, action):
        raise Forbidden("FORBIDDEN", f"Недостаточно прав: {what} недоступно роли «{role_label(user.role)}».",
                        details={"action": action, "role": user.role})


# ================================================================== видимость
def can_view_defect(db: Session, user: User, r: DefectReport) -> bool:
    sc = scope_of(db, user)
    if can(user, "defect.view_station") and sc.station_id == r.station_id:
        return True
    if can(user, "defect.view_own") and r.author_id == user.id:
        return True
    # исполнитель или проверяющий связанных работ видит исходное сообщение
    wos = db.execute(select(WorkOrder).where(WorkOrder.defect_report_id == r.id)).scalars()
    return any(_wo_personal(db, user, w) for w in wos)


def _wo_personal(db: Session, user: User, w: WorkOrder) -> bool:
    if not can(user, "work_order.execute"):
        return False
    if w.assignee_id == user.id or user.id in (w.executed_by or []):
        return True
    sc = scope_of(db, user)
    return bool(w.brigade_id and sc.brigade_id == w.brigade_id and w.status not in ("completed", "cancelled")
                and w.station_id == sc.station_id)


def can_view_wo(db: Session, user: User, w: WorkOrder) -> bool:
    sc = scope_of(db, user)
    if sc.station_id == w.station_id and (can(user, "defect.view_station") or can(user, "work_order.inspect")
                                          or can(user, "work_order.assign")):
        return True
    if _wo_personal(db, user, w):
        return True
    if w.defect_report_id and can(user, "defect.view_own"):
        r = db.get(DefectReport, w.defect_report_id)
        if r and r.author_id == user.id:
            return True
    return False


def get_defect(db: Session, user: User, rid: str, *, lock: bool = False) -> DefectReport:
    r = db.get(DefectReport, rid, with_for_update=lock)
    if not r or not can_view_defect(db, user, r):
        raise NotFound("DEFECT_NOT_FOUND", "Сообщение не найдено.")  # чужие сообщения неотличимы от несуществующих
    return r


def get_wo(db: Session, user: User, wid: str, *, lock: bool = False) -> WorkOrder:
    w = db.get(WorkOrder, wid, with_for_update=lock)
    if not w or not can_view_wo(db, user, w):
        raise NotFound("WORK_ORDER_NOT_FOUND", "Заявка на работы не найдена.")
    return w


# ================================================================== состояние вагона
def recompute_wagon(db: Session, w: Wagon) -> str:
    """condition = ok только когда нет неустранённых блокирующих дефектов, ограничений до проверки и
    активных сценарных инцидентов. Замена в составе не делает снятый вагон исправным."""
    db.flush()
    open_fault = db.execute(select(func.count()).select_from(DefectReport).where(
        DefectReport.wagon_id == w.id, DefectReport.status == "accepted", DefectReport.fault_open.is_(True),
        DefectReport.blocking.is_(True))).scalar()
    restricted = db.execute(select(func.count()).select_from(DefectReport).where(
        DefectReport.wagon_id == w.id, DefectReport.restriction_active.is_(True))).scalar()
    # сценарные инциденты (демо, диагностика); инцидент-запись сообщения диспетчера не считается отдельно
    legacy = sum(1 for i in db.execute(select(Incident).where(
        Incident.kind == "faulty_wagon", Incident.status == "active", Incident.object_id == w.id)).scalars()
        if (i.params or {}).get("mode") != "workflow")
    if open_fault or legacy:
        new = "faulty" if w.train_id else "in_repair"
    elif restricted:
        new = "restricted"
    else:
        new = "ok"
    if new != w.condition:
        old = w.condition
        w.condition = new
        domain_event(db, "wagon.condition", f"Вагон № {w.number}: {WAGON_CONDITION.get(old, old)} → {WAGON_CONDITION[new]}",
                     payload={"wagon_id": w.id, "from": old, "to": new}, model_time=_model_now(db))
        bump(db, "wagon:condition")  # изменились ограничения модели — конфликты и план пересчитываются
    return new


# ================================================================== вложения
def link_attachments(db: Session, user: User, ids: list[str], owner_type: str, owner_id: str):
    for aid in ids or []:
        a = db.get(Attachment, aid, with_for_update=True)
        if not a or a.uploader_id != user.id:
            raise AppError("ATTACHMENT_NOT_FOUND", "Вложение не найдено или загружено другим пользователем.",
                           details={"attachment_id": aid})
        if a.owner_id and (a.owner_type, a.owner_id) != (owner_type, owner_id):
            raise Conflict("ATTACHMENT_LINKED", "Вложение уже прикреплено к другой записи.", details={"attachment_id": aid})
        a.owner_type, a.owner_id = owner_type, owner_id


def attachments_of(db: Session, owner_type: str, owner_id: str) -> list[dict]:
    return [{"id": a.id, "name": a.original_name, "content_type": a.content_type, "size": a.size,
             "uploaded_at": iso(a.created_at), "url": f"/api/v1/attachments/{a.id}"}
            for a in db.execute(select(Attachment).where(Attachment.owner_type == owner_type,
                                                         Attachment.owner_id == owner_id)
                                .order_by(Attachment.created_at)).scalars()]


# ================================================================== сообщения о дефектах
def _report_hash(body: dict) -> str:
    keys = ("wagon_id", "wagon_number", "train_id", "track_id", "position", "category", "component", "description",
            "urgency", "attachment_ids", "location")
    return hashlib.sha256(json.dumps({k: body.get(k) for k in keys}, sort_keys=True, default=str).encode()).hexdigest()


def resolve_wagon(db: Session, station_id: str, wagon_id: str | None, number: str | None) -> Wagon | None:
    """Вагон ищется только среди вагонов станции. Неизвестный номер не создаёт вагон в БД."""
    if wagon_id:
        w = db.get(Wagon, wagon_id)
        if not w:
            raise AppError("WAGON_NOT_FOUND", "Выбранный вагон не найден. Укажите номер вручную.",
                           details={"field": "wagon_id"})
        return w
    if number:
        return db.execute(select(Wagon).where(Wagon.number == number.strip())).scalars().first()
    return None


def submit_defect(db: Session, user: User, body: dict) -> tuple[dict, bool]:
    """Создаёт сообщение. Повтор с тем же client_uuid — тот же результат (идемпотентно, без срока
    давности: офлайн-очередь может отправить сообщение через несколько дней). (dict, replay)."""
    _need(user, "defect.create", "сообщение о дефекте")
    h = _report_hash(body)
    old = db.execute(select(DefectReport).where(DefectReport.author_id == user.id,
                                                DefectReport.client_uuid == body["client_uuid"])).scalar_one_or_none()
    if old:
        if old.content_hash != h:
            raise Conflict("IDEMPOTENCY_KEY_REUSED", "Этот идентификатор сообщения уже использован для другого содержимого.",
                           hint="Создайте новое сообщение.")
        return defect_view(db, user, old), True
    if body["category"] not in CATEGORIES:
        raise AppError("BAD_CATEGORY", "Неизвестная категория дефекта.", details={"field": "category"})
    if body["urgency"] not in URGENCY:
        raise AppError("BAD_URGENCY", "Неизвестная срочность.", details={"field": "urgency"})
    sc = scope_of(db, user)
    w = resolve_wagon(db, sc.station_id, body.get("wagon_id"), body.get("wagon_number"))
    train = db.get(Train, w.train_id) if w and w.train_id else (db.get(Train, body["train_id"]) if body.get("train_id") else None)
    track_id = body.get("track_id") or (train.current_track_id if train else (w.track_id if w else None))
    if track_id and not db.get(Track, track_id):
        raise AppError("TRACK_NOT_FOUND", "Путь не найден.", details={"field": "track_id"})
    now = utcnow()
    num = (db.execute(select(func.coalesce(func.max(DefectReport.number), 0))).scalar() or 0) + 1
    r = DefectReport(id=_uid("DR"), number=num, client_uuid=body["client_uuid"], content_hash=h, station_id=sc.station_id,
                     pto_id=sc.pto_id, author_id=user.id, wagon_id=w.id if w else None,
                     wagon_number_raw=(body.get("wagon_number") or (w.number if w else "")).strip()[:16],
                     train_id=train.id if train else None, track_id=track_id,
                     position=body.get("position") or (w.position if w and w.train_id else None),
                     category=body["category"], component=body.get("component"), description=body["description"].strip(),
                     urgency=body["urgency"], status="submitted", location=body.get("location"),
                     client_created_at=body.get("client_created_at"), created_at=now, updated_at=now, version=1)
    if not r.wagon_number_raw:
        raise AppError("WAGON_REQUIRED", "Укажите номер вагона.", details={"field": "wagon_number"})
    db.add(r)
    db.flush()
    link_attachments(db, user, body.get("attachment_ids") or [], "defect", r.id)
    _event(db, "defect", r.id, user, "status", None, "submitted",
           f"Сообщение отправлено: {CATEGORIES[r.category]}, срочность «{URGENCY[r.urgency]}»" +
           ("" if w else ". Вагон с таким номером не найден — требуется привязка"))
    if r.urgency == "critical" and w:
        # временное ограничение до проверки: вагон блокирует отправление в модели
        r.restriction_active = True
        recompute_wagon(db, w)
    audit(db, user, "defect.submit", "defect_report", r.id,
          f"Сообщение № {r.number} о дефекте вагона № {r.wagon_number_raw} ({URGENCY[r.urgency].lower()})",
          after={"wagon_id": r.wagon_id, "category": r.category, "urgency": r.urgency, "restriction": r.restriction_active},
          model_time=_model_now(db))
    title = f"{'⚠ ' if r.urgency != 'normal' else ''}Сообщение № {r.number}: вагон № {r.wagon_number_raw}"
    notify(db, users_with(db, "defect.view_station", r.station_id), "defect.new", title,
           f"{CATEGORIES[r.category]}. {r.description[:140]}", "defect", r.id, exclude=user.id)
    _changed(db, "defect", r.id, r.station_id)
    return defect_view(db, user, r), False


def _transition_defect(db, user: User, r: DefectReport, to: str, text: str = "", *, author_ok: bool = False):
    key = (r.status, to)
    if key not in DEFECT_TRANSITIONS:
        raise Conflict("INVALID_TRANSITION", f"Переход «{DEFECT_STATUS[r.status]}» → «{DEFECT_STATUS[to]}» недопустим.",
                       details={"from": r.status, "to": to})
    need = DEFECT_TRANSITIONS[key]
    ok = any(can(user, p) for p in need.replace("|author", "").split("|")) or (author_ok and "author" in need and r.author_id == user.id)
    if not ok:
        raise Forbidden("FORBIDDEN", "Недостаточно прав для этого действия с сообщением.", details={"action": need})
    frm = r.status
    r.status = to
    r.updated_at = utcnow()
    r.version += 1
    _event(db, "defect", r.id, user, "status", frm, to, text)
    return frm


def defect_action(db: Session, user: User, rid: str, action: str, body: dict) -> dict:
    r = get_defect(db, user, rid, lock=True)
    _check_version(r, body.get("expected_version"))
    reason = (body.get("reason") or "").strip()
    now = utcnow()
    if action == "acknowledge":
        _transition_defect(db, user, r, "acknowledged", "Получение подтверждено")
        r.acknowledged_at, r.acknowledged_by = now, user.id
        notify(db, [r.author_id], "defect.ack", f"Сообщение № {r.number} принято к рассмотрению",
               f"Получение подтвердил: {user.full_name}", "defect", r.id)
    elif action == "review":
        _transition_defect(db, user, r, "under_review", reason or "Взято на рассмотрение")
        if not r.acknowledged_at:
            r.acknowledged_at, r.acknowledged_by = now, user.id
    elif action == "needs_info":
        if not reason:
            raise AppError("REASON_REQUIRED", "Опишите, какие сведения нужно уточнить.", details={"field": "reason"})
        _transition_defect(db, user, r, "needs_info", reason)
        if not r.acknowledged_at:
            r.acknowledged_at, r.acknowledged_by = now, user.id
        notify(db, [r.author_id], "defect.needs_info", f"Уточните сообщение № {r.number}", reason, "defect", r.id)
    elif action == "reply":
        if r.author_id != user.id and not can(user, "defect.triage"):
            raise Forbidden("FORBIDDEN", "Ответить на запрос уточнения может автор сообщения.")
        if not reason:
            raise AppError("TEXT_REQUIRED", "Напишите ответ.", details={"field": "reason"})
        link_attachments(db, user, body.get("attachment_ids") or [], "defect", r.id)
        _transition_defect(db, user, r, "under_review", f"Ответ на уточнение: {reason}", author_ok=True)
        notify(db, users_with(db, "defect.triage", r.station_id), "defect.reply",
               f"Ответ на уточнение по сообщению № {r.number}", reason, "defect", r.id, exclude=user.id)
    elif action == "reject":
        if not reason:
            raise AppError("REASON_REQUIRED", "Для отклонения укажите основание.", details={"field": "reason"})
        if r.urgency == "critical" or r.restriction_active:
            # снятие ограничения критичного сообщения — только принимающий решение
            _need(user, "work_order.create", "отклонение предположительно критического сообщения и снятие ограничения")
        _transition_defect(db, user, r, "rejected", reason)
        r.decision_reason = reason
        r.restriction_active = False
        _recompute_by_report(db, r)
        notify(db, [r.author_id], "defect.rejected", f"Сообщение № {r.number} отклонено", reason, "defect", r.id)
    elif action == "duplicate":
        orig_id = body.get("duplicate_of")
        if not orig_id or not reason:
            raise AppError("DUPLICATE_REQUIRED", "Укажите исходное сообщение и основание объединения.",
                           details={"field": "duplicate_of"})
        orig = db.get(DefectReport, orig_id)
        if not orig or orig.id == r.id or orig.station_id != r.station_id or orig.status in ("duplicate", "rejected"):
            raise AppError("BAD_DUPLICATE", "Исходное сообщение не найдено, совпадает с текущим или само закрыто.",
                           details={"field": "duplicate_of"})
        if r.urgency == "critical" and r.restriction_active and not orig.restriction_active and orig.status != "accepted":
            _need(user, "work_order.create", "объединение критичного сообщения со снятием ограничения")
        _transition_defect(db, user, r, "duplicate", f"Дубликат сообщения № {orig.number}: {reason}")
        r.duplicate_of, r.decision_reason = orig.id, reason
        # фотографии и ограничение переходят к исходному сообщению
        if r.restriction_active and orig.wagon_id == r.wagon_id and orig.status != "accepted":
            orig.restriction_active = True
        r.restriction_active = False
        _event(db, "defect", orig.id, user, "link", text=f"Присоединено сообщение № {r.number} (дубликат)")
        _recompute_by_report(db, r)
        notify(db, [r.author_id], "defect.duplicate", f"Сообщение № {r.number} объединено с № {orig.number}", reason,
               "defect", r.id)
    elif action == "link_wagon":
        _need(user, "defect.triage", "привязка сообщения к вагону")
        if r.status not in DEFECT_OPEN:
            raise Conflict("INVALID_STATE", "Привязка возможна только до решения по сообщению.")
        w = db.get(Wagon, body.get("wagon_id") or "")
        if not w:
            raise AppError("WAGON_NOT_FOUND", "Вагон не найден.", details={"field": "wagon_id"})
        prev = r.wagon_id
        r.wagon_id, r.train_id = w.id, w.train_id
        r.position = w.position if w.train_id else None
        r.version += 1
        r.updated_at = now
        _event(db, "defect", r.id, user, "link", text=f"Привязано к вагону № {w.number}" + (f" ({reason})" if reason else ""))
        if r.urgency == "critical":
            r.restriction_active = True
        recompute_wagon(db, w)
        if prev and prev != w.id:
            pw = db.get(Wagon, prev)
            if pw:
                recompute_wagon(db, pw)
    elif action == "comment":
        if not reason:
            raise AppError("TEXT_REQUIRED", "Напишите замечание.", details={"field": "reason"})
        link_attachments(db, user, body.get("attachment_ids") or [], "defect", r.id)
        _event(db, "defect", r.id, user, "comment", text=reason)
        r.updated_at = now
        targets = [r.author_id] + users_with(db, "defect.triage", r.station_id)
        notify(db, targets, "defect.comment", f"Замечание к сообщению № {r.number}", reason, "defect", r.id, exclude=user.id)
    else:
        raise AppError("UNKNOWN_ACTION", f"Неизвестное действие: {action}")
    audit(db, user, f"defect.{action}", "defect_report", r.id, f"Сообщение № {r.number}: {action}", reason=reason or None,
          after={"status": r.status, "restriction": r.restriction_active}, model_time=_model_now(db))
    _changed(db, "defect", r.id, r.station_id)
    return defect_view(db, user, r)


def _close_mirror_incident(db: Session, r: DefectReport, user: User | None, why: str):
    """Инцидент-запись сообщения диспетчера закрывается вместе с сообщением (не раньше)."""
    for inc in db.execute(select(Incident).where(Incident.kind == "faulty_wagon", Incident.status == "active")).scalars():
        if (inc.params or {}).get("defect_report_id") == r.id:
            inc.status = "resolved"
            inc.end_at = _model_now(db)
            domain_event(db, "incident.resolved", f"Закрыт: {inc.title} ({why})", payload={"incident_id": inc.id},
                         model_time=_model_now(db))
            bump(db, "incident:resolved")


def _recompute_by_report(db: Session, r: DefectReport):
    if r.status in ("rejected", "duplicate") or (r.status == "accepted" and not r.fault_open and not r.restriction_active):
        _close_mirror_incident(db, r, None, DEFECT_STATUS[r.status].lower())
    if r.wagon_id:
        w = db.get(Wagon, r.wagon_id)
        if w:
            recompute_wagon(db, w)


# ================================================================== решение и заявки на работы
def _depot_track(db: Session) -> Track | None:
    return db.execute(select(Track).where(Track.kind == "repair")).scalars().first()


def replacement_candidates(db: Session, wagon: Wagon) -> dict:
    """Подбор исправного вагона для замены: тот же род, порожний, исправный, вне состава и не
    зарезервирован другой заявкой. Гружёный неисправный вагон заменить переключением нельзя."""
    reasons = []
    if wagon.loaded:
        reasons.append("Неисправный вагон гружёный: замена требует отдельного согласованного процесса перегрузки груза "
                       "(в демо-версии не поддерживается). Выберите ремонт без отцепки, отцепку и ремонт или передачу.")
    reserved = {w for (w,) in db.execute(select(WorkOrder.replacement_wagon_id).where(
        WorkOrder.replacement_wagon_id.is_not(None), WorkOrder.status.not_in(["completed", "cancelled"])))}
    out = []
    for c in db.execute(select(Wagon).where(Wagon.train_id.is_(None), Wagon.id != wagon.id)
                        .order_by(Wagon.number)).scalars():
        why = []
        if c.kind != wagon.kind:
            why.append("другой род вагона")
        if c.condition != "ok":
            why.append("не исправен")
        if c.loaded:
            why.append("гружёный")
        if c.id in reserved:
            why.append("зарезервирован другой заявкой на замену")
        if not c.track_id:
            why.append("не на станции")
        out.append({"id": c.id, "number": c.number, "kind": c.kind, "loaded": c.loaded, "condition": c.condition,
                    "track_id": c.track_id, "suitable": not why and not wagon.loaded, "reasons": why})
    suitable = [c for c in out if c["suitable"]]
    if not wagon.loaded and not suitable:
        reasons.append("Нет подходящего исправного вагона того же рода (порожнего, незарезервированного) на станции.")
    return {"wagon_id": wagon.id, "candidates": out, "suitable": len(suitable), "problems": reasons}


def _make_station_op(db: Session, wo: WorkOrder, w: Wagon, kind_note: str, duration: int, target_track: str) -> Operation:
    """Технологическая операция по заявке (отцепка или замена). reserved=False — планировщик
    размещает её в ресурсах и времени; движок выполняет в модельном времени и меняет состав."""
    train = db.get(Train, w.train_id)
    ops = list(db.execute(select(Operation).where(Operation.train_id == train.id).order_by(Operation.seq)).scalars())
    dep = next((o for o in ops if o.kind == "departure" and o.status in ("planned", "confirmed")), None)
    if train.status not in ("on_station", "waiting", "approaching", "scheduled"):
        raise Conflict("TRAIN_NOT_ON_STATION", f"Поезд № {train.number} не на станции: операция по составу невозможна.")
    now = _model_now(db) or utcnow()
    prev_end = max((aware(o.planned_end) for o in ops if dep is None or o.seq < dep.seq), default=now)
    start = max(now + timedelta(minutes=5), prev_end)
    seq = dep.seq if dep else (max((o.seq for o in ops), default=0) + 1)
    op = Operation(id=_uid("OP"), station_id=wo.station_id, train_id=train.id, kind="uncoupling", seq=seq,
                   track_id=target_track, from_track_id=train.current_track_id or (dep.track_id if dep else None), side="east",
                   duration_min=duration, requirements=["shunting_loco", "loco_crew", "shunting_crew"], resource_ids=[],
                   route_nodes=[], planned_start=start, planned_end=start + timedelta(minutes=duration), status="planned",
                   reserved=False, note=kind_note[:255], work_order_id=wo.id)
    if dep:
        for o in ops:
            if o.seq >= seq:
                o.seq += 1
    db.add(op)
    return op


def create_work_order(db: Session, user: User, r: DefectReport, body: dict) -> WorkOrder:
    kind = body.get("kind")
    if kind not in WO_KIND:
        raise AppError("BAD_KIND", "Выберите вид работ.", details={"field": "kind"})
    _need(user, "work_order.create", "создание заявки на работы")
    if not r.wagon_id:
        raise Conflict("WAGON_NOT_LINKED", "Сообщение не привязано к вагону: сначала выполните привязку.",
                       hint="Найдите вагон по номеру и привяжите сообщение (действие «Привязать вагон»).")
    w = db.get(Wagon, r.wagon_id, with_for_update=True)
    depot = _depot_track(db)
    now = utcnow()
    num = (db.execute(select(func.coalesce(func.max(WorkOrder.number), 0))).scalar() or 0) + 1
    wo = WorkOrder(id=_uid("WO"), number=num, station_id=r.station_id, pto_id=r.pto_id, defect_report_id=r.id,
                   wagon_id=w.id, kind=kind, status="created", brigade_id=body.get("brigade_id"),
                   actions=[a for a in (body.get("actions") or []) if a][:20],
                   due_at=body.get("due_at") or (now + timedelta(hours=4 if r.urgency != "normal" else 24)),
                   created_by=user.id, created_at=now, updated_at=now, version=1, executed_by=[], operation_ids=[])
    if kind == "uncoupling_repair":
        if not depot:
            raise Conflict("NO_REPAIR_TRACK", "На станции нет ремонтного пути: отцепка с подачей в депо невозможна.",
                           details={"options": ["repair_in_place", "transfer"]},
                           hint="Варианты: ремонт без отцепки (если допустим) или передача на другую ремонтную площадку.")
        if w.train_id:
            db.add(wo)
            db.flush()
            op = _make_station_op(db, wo, w, f"Отцепка неисправного вагона № {w.number} (заявка № {num})", 20, depot.id)
            wo.operation_ids = [op.id]
    elif kind == "replacement":
        _need(user, "wagon_replacement.approve", "согласование замены вагона")
        if not w.train_id:
            raise Conflict("NOT_IN_TRAIN", "Вагон не в составе: замена в составе не требуется.")
        cand = replacement_candidates(db, w)
        rid = body.get("replacement_wagon_id")
        if w.loaded:
            raise Conflict("LOADED_WAGON", cand["problems"][0], details=cand)
        c = next((x for x in cand["candidates"] if x["id"] == rid), None)
        if not c or not c["suitable"]:
            raise Conflict("REPLACEMENT_UNSUITABLE",
                           "Выбранный вагон не подходит для замены." if c else "Укажите исправный вагон для замены.",
                           details={**cand, "selected": c})
        rw = db.get(Wagon, rid, with_for_update=True)
        wo.replacement_wagon_id = rw.id
        db.add(wo)
        try:
            with db.begin_nested():
                db.flush()  # уникальный индекс: вагон не может быть в двух незавершённых заменах
        except IntegrityError:
            raise Conflict("REPLACEMENT_RESERVED", f"Вагон № {rw.number} уже зарезервирован другой заявкой на замену.",
                           hint="Выберите другой исправный вагон.") from None
        op = _make_station_op(db, wo, w, f"Замена вагона № {w.number} на № {rw.number}: отцепка, подача, прицепка "
                                         f"(заявка № {num})", 35, (depot.id if depot else rw.track_id))
        wo.operation_ids = [op.id]
    if wo not in db:
        db.add(wo)
    db.flush()
    if kind != "inspection":
        r.fault_open = True  # неисправность подтверждена решением
    _event(db, "work_order", wo.id, user, "status", None, "created",
           f"{WO_KIND[kind]} по сообщению № {r.number}" + (f". Основание: {body.get('reason')}" if body.get("reason") else ""))
    _event(db, "defect", r.id, user, "link", text=f"Создана заявка на работы № {num}: {WO_KIND[kind].lower()}")
    audit(db, user, "work_order.create", "work_order", wo.id, f"Заявка № {num}: {WO_KIND[kind]} (вагон № {w.number})",
          after={"kind": kind, "defect": r.id, "replacement": wo.replacement_wagon_id, "operations": wo.operation_ids},
          reason=body.get("reason"), model_time=_model_now(db))
    if body.get("assignee_id"):
        _assign(db, user, wo, body["assignee_id"], body.get("brigade_id"))
    if wo.operation_ids:
        bump(db, f"work_order:{kind}")
    _changed(db, "work_order", wo.id, wo.station_id)
    return wo


def decide(db: Session, user: User, rid: str, body: dict) -> dict:
    """Решение по сообщению: принять с заявкой на работы."""
    r = get_defect(db, user, rid, lock=True)
    _check_version(r, body.get("expected_version"))
    _need(user, "work_order.create", "решение по сообщению")
    if r.status not in ("acknowledged", "under_review"):
        raise Conflict("INVALID_TRANSITION", f"Решение возможно для сообщения на рассмотрении (сейчас: {DEFECT_STATUS[r.status]}).")
    wo = create_work_order(db, user, r, body)
    _transition_defect(db, user, r, "accepted", f"Решение: {WO_KIND[wo.kind].lower()}" +
                       (f". {body.get('reason')}" if body.get("reason") else ""))
    r.decision, r.decision_reason = wo.kind, body.get("reason")
    if wo.kind != "inspection":
        r.restriction_active = False  # ограничение до проверки заменено подтверждённой неисправностью
    _recompute_by_report(db, r)
    notify(db, [r.author_id], "defect.accepted", f"Сообщение № {r.number} принято: {WO_KIND[wo.kind].lower()}",
           f"Заявка на работы № {wo.number}", "defect", r.id)
    audit(db, user, "defect.accept", "defect_report", r.id, f"Сообщение № {r.number} принято: {WO_KIND[wo.kind]}",
          reason=body.get("reason"), after={"work_order": wo.id}, model_time=_model_now(db))
    _changed(db, "defect", r.id, r.station_id)
    return {"defect": defect_view(db, user, r), "work_order": wo_view(db, user, wo)}


def add_work_order(db: Session, user: User, rid: str, body: dict) -> dict:
    """Дополнительная заявка по принятому сообщению (например, ремонт снятого при замене вагона)."""
    r = get_defect(db, user, rid, lock=True)
    if r.status != "accepted":
        raise Conflict("INVALID_STATE", "Дополнительная заявка создаётся только по принятому сообщению.")
    wo = create_work_order(db, user, r, body)
    _recompute_by_report(db, r)
    return wo_view(db, user, wo)


def _assign(db: Session, user: User, wo: WorkOrder, assignee_id: str, brigade_id: str | None):
    a = db.get(User, assignee_id)
    if not a or not a.active or "work_order.execute" not in role_permissions(a.role):
        raise AppError("BAD_ASSIGNEE", "Исполнитель не найден или не может выполнять работы.", details={"field": "assignee_id"})
    if wo.kind != "inspection" and a.role == "wagon_inspector":
        raise AppError("BAD_ASSIGNEE", "Осмотрщик вагонов выполняет только осмотры: назначьте осмотрщика-ремонтника или слесаря.",
                       details={"field": "assignee_id"})
    asc, usc = scope_of(db, a), scope_of(db, user)
    if asc.station_id != wo.station_id:
        raise AppError("BAD_ASSIGNEE", "Исполнитель привязан к другой станции.", details={"field": "assignee_id"})
    if not can(user, "work_order.create") and usc.pto_id and asc.pto_id != usc.pto_id:
        raise Forbidden("FORBIDDEN", "Старший осмотрщик распределяет задания только между исполнителями своей зоны.")
    key = (wo.status, "assigned")
    if key not in WO_TRANSITIONS:
        raise Conflict("INVALID_TRANSITION", f"Назначение невозможно в состоянии «{WO_STATUS[wo.status]}».")
    frm = wo.status
    wo.assignee_id = a.id
    wo.brigade_id = brigade_id or asc.brigade_id or wo.brigade_id
    wo.status = "assigned"
    wo.version += 1
    wo.updated_at = utcnow()
    _event(db, "work_order", wo.id, user, "assign", frm, "assigned", f"Исполнитель: {a.full_name}")
    notify(db, [a.id], "work_order.assigned", f"Назначено задание № {wo.number}: {WO_KIND[wo.kind].lower()}",
           f"Срок: {iso(wo.due_at)}" if wo.due_at else "", "work_order", wo.id)


def _station_ops_state(db: Session, wo: WorkOrder) -> tuple[bool, str]:
    """Готовность места работ: для отцепки и замены операция по составу должна быть выполнена."""
    if not wo.operation_ids:
        return True, ""
    ops = [db.get(Operation, oid) for oid in wo.operation_ids]
    pending = [o for o in ops if o and o.status != "done"]
    if pending:
        o = pending[0]
        st = {"planned": "ещё не спланирована" if not o.reserved else "запланирована",
              "confirmed": "запланирована", "in_progress": "выполняется", "cancelled": "отменена"}.get(o.status, o.status)
        return False, f"Операция по составу «{o.note}» {st}" + (f" на {iso(o.planned_start)}" if o.reserved and o.status != "in_progress" else "")
    return True, ""


def wo_action(db: Session, user: User, wid: str, action: str, body: dict) -> dict:
    wo = get_wo(db, user, wid, lock=True)
    _check_version(wo, body.get("expected_version"))
    text = (body.get("reason") or body.get("comment") or "").strip()
    now = utcnow()
    r = db.get(DefectReport, wo.defect_report_id) if wo.defect_report_id else None

    def go(to: str, note: str):
        key = (wo.status, to)
        if key not in WO_TRANSITIONS:
            raise Conflict("INVALID_TRANSITION", f"Переход «{WO_STATUS[wo.status]}» → «{WO_STATUS.get(to, to)}» недопустим.",
                           details={"from": wo.status, "to": to})
        _need(user, WO_TRANSITIONS[key], f"действие «{note}»")
        frm = wo.status
        wo.status = to
        wo.version += 1
        wo.updated_at = now
        _event(db, "work_order", wo.id, user, "status", frm, to, (note + (f": {text}" if text else "")))

    def executor_only():
        if wo.assignee_id != user.id:
            raise Forbidden("NOT_ASSIGNEE", "Действие доступно назначенному исполнителю задания.")
        if wo.kind != "inspection" and user.role == "wagon_inspector":
            raise Forbidden("FORBIDDEN", "Осмотрщик вагонов выполняет только осмотры.")

    if action == "assign":
        _need(user, "work_order.assign", "назначение исполнителя")
        _assign(db, user, wo, body.get("assignee_id") or "", body.get("brigade_id"))
    elif action == "start":
        executor_only()
        ready, why = _station_ops_state(db, wo)
        if not ready:
            raise Conflict("PLACE_NOT_READY", f"Начать работы нельзя: {why}.",
                           hint="Работы по вагону начинаются после выполнения операции по составу.")
        go("in_progress", "Работы начаты" if wo.status == "assigned" else "Работы возобновлены после доработки")
        wo.started_at = wo.started_at or now
        if user.id not in (wo.executed_by or []):
            wo.executed_by = [*(wo.executed_by or []), user.id]
    elif action == "pause":
        executor_only()
        if not text:
            raise AppError("REASON_REQUIRED", "Укажите причину приостановки.", details={"field": "reason"})
        wo.hold_from, wo.hold_reason = wo.status, text
        go("on_hold", "Приостановлено")
    elif action == "resume":
        executor_only()
        if wo.status != "on_hold":
            raise Conflict("INVALID_TRANSITION", "Задание не приостановлено.")
        back = wo.hold_from or "assigned"
        frm = wo.status
        _need(user, "work_order.execute", "возобновление работ")
        wo.status, wo.hold_from, wo.hold_reason = back, None, None
        wo.version += 1
        wo.updated_at = now
        _event(db, "work_order", wo.id, user, "status", frm, back, "Возобновлено")
    elif action == "submit":
        executor_only()
        rep = (body.get("report") or "").strip()
        if not rep:
            raise AppError("REPORT_REQUIRED", "Опишите выполненные работы.", details={"field": "report"})
        if wo.kind == "replacement":
            ready, why = _station_ops_state(db, wo)
            if not ready:
                raise Conflict("PLACE_NOT_READY", f"Замена в составе ещё не выполнена: {why}.")
        wo.report, wo.materials = rep, (body.get("materials") or "").strip() or None
        link_attachments(db, user, body.get("attachment_ids") or [], "work_order", wo.id)
        go("awaiting_inspection", "Результат передан на контрольный осмотр")
        wo.submitted_at = now
        notify(db, users_with(db, "work_order.inspect", wo.station_id) + users_with(db, "work_order.create", wo.station_id),
               "work_order.inspection", f"Задание № {wo.number} ожидает контрольного осмотра", rep[:140],
               "work_order", wo.id, exclude=user.id)
    elif action in ("accept", "rework"):
        _need(user, "work_order.inspect", "контрольный осмотр")
        if user.id == wo.assignee_id or user.id in (wo.executed_by or []):
            raise Forbidden("SELF_INSPECTION", "Нельзя принять работу, которую выполняли вы сами: нужен другой проверяющий.")
        sc = scope_of(db, user)
        if sc.station_id != wo.station_id:
            raise Forbidden("FORBIDDEN", "Заявка другой станции.")
        if action == "rework" and not text:
            raise AppError("REASON_REQUIRED", "Опишите замечания для доработки.", details={"field": "reason"})
        db.add(WorkInspection(work_order_id=wo.id, inspector_id=user.id, result="accepted" if action == "accept" else "rework",
                              comment=text, created_at=now))
        if action == "rework":
            go("rework", "Возвращено на доработку")
            notify(db, [wo.assignee_id], "work_order.rework", f"Задание № {wo.number} возвращено на доработку", text,
                   "work_order", wo.id)
        else:
            go("completed", "Результат принят по контрольному осмотру")
            wo.completed_at = now
            _complete_effects(db, user, wo, r)
            notify(db, ([r.author_id] if r else []) + [wo.assignee_id] + users_with(db, "work_order.create", wo.station_id),
                   "work_order.completed", f"Задание № {wo.number} выполнено и принято", WO_KIND[wo.kind],
                   "work_order", wo.id, exclude=user.id)
    elif action == "cancel":
        if not text:
            raise AppError("REASON_REQUIRED", "Для отмены укажите основание.", details={"field": "reason"})
        ops = [db.get(Operation, oid) for oid in wo.operation_ids or []]
        busy = [o for o in ops if o and o.status in ("in_progress", "done")]
        if busy:
            raise Conflict("DEPENDENT_OPERATION", f"Отмена невозможна: операция «{busy[0].note}» уже "
                                                  f"{'выполняется' if busy[0].status == 'in_progress' else 'выполнена'}.",
                           hint="Создайте новую заявку на обратную операцию или дождитесь завершения.")
        go("cancelled", "Заявка отменена")
        wo.cancel_reason = text
        for o in ops:
            if o and o.status in ("planned", "confirmed"):
                o.status = "cancelled"
        if ops:
            bump(db, "work_order:cancel")
        notify(db, [wo.assignee_id], "work_order.cancelled", f"Задание № {wo.number} отменено", text, "work_order", wo.id)
    elif action == "comment":
        if not text:
            raise AppError("TEXT_REQUIRED", "Напишите замечание.", details={"field": "reason"})
        link_attachments(db, user, body.get("attachment_ids") or [], "work_order", wo.id)
        _event(db, "work_order", wo.id, user, "comment", text=text)
        wo.updated_at = now
    else:
        raise AppError("UNKNOWN_ACTION", f"Неизвестное действие: {action}")
    audit(db, user, f"work_order.{action}", "work_order", wo.id, f"Заявка № {wo.number}: {action}", reason=text or None,
          after={"status": wo.status, "assignee": wo.assignee_id}, model_time=_model_now(db))
    _changed(db, "work_order", wo.id, wo.station_id)
    return wo_view(db, user, wo)


def _complete_effects(db: Session, user: User, wo: WorkOrder, r: DefectReport | None):
    """Принятый результат снимает только СВОЙ дефект. Состояние вагона пересчитывается по всем
    его дефектам: несколько дефектов одного вагона не снимаются приёмкой одного задания."""
    w = db.get(Wagon, wo.wagon_id)
    if r:
        if wo.kind == "inspection":
            r.restriction_active = False
        elif wo.kind in ("repair_in_place", "uncoupling_repair", "transfer"):
            r.fault_open = False
            r.restriction_active = False
            _event(db, "defect", r.id, user, "status", text=f"Неисправность устранена (заявка № {wo.number}, контрольный осмотр)")
            _close_mirror_incident(db, r, user, f"неисправность устранена по заявке № {wo.number}")
        # замена: снятый вагон остаётся неисправным до собственного ремонта и приёмки
        r.version += 1
    if w:
        recompute_wagon(db, w)
    if wo.replacement_wagon_id:
        rw = db.get(Wagon, wo.replacement_wagon_id)
        if rw:
            recompute_wagon(db, rw)


def apply_station_operation(db: Session, op: Operation, model_now: datetime) -> str | None:
    """Вызывается движком по завершении операции заявки. Атомарно меняет состав: принадлежность,
    порядок, количество и длину; местонахождение снятого вагона. Заявка сама состав не меняет."""
    wo = db.get(WorkOrder, op.work_order_id)
    if not wo or wo.status == "cancelled":
        return None
    w = db.get(Wagon, wo.wagon_id)
    train = db.get(Train, op.train_id) if op.train_id else None
    if not w or not train or w.train_id != train.id:
        return None
    pos = w.position
    w.train_id, w.track_id, w.position = None, op.track_id, 0
    msg = f"Вагон № {w.number} отцеплен от поезда № {train.number}"
    if wo.kind == "replacement" and wo.replacement_wagon_id:
        rw = db.get(Wagon, wo.replacement_wagon_id)
        if rw and rw.train_id is None and rw.condition == "ok":
            rw.train_id, rw.position, rw.track_id = train.id, pos, None
            msg = f"Вагон № {w.number} заменён в составе поезда № {train.number} на исправный № {rw.number}"
        else:
            train.wagons_count = max(0, train.wagons_count - 1)
            msg += " (вагон для замены недоступен — прицепка не выполнена)"
    else:
        train.wagons_count = max(0, train.wagons_count - 1)
        rest = sorted([x for x in db.execute(select(Wagon).where(Wagon.train_id == train.id)).scalars()], key=lambda x: x.position)
        for i, x in enumerate(rest, 1):
            x.position = i
    train.length_m = None  # длина пересчитывается по фактическим вагонам
    recompute_wagon(db, w)
    _event(db, "work_order", wo.id, None, "comment", text=msg + " (операция по составу выполнена)")
    if wo.assignee_id:
        notify(db, [wo.assignee_id], "work_order.place_ready", f"Задание № {wo.number}: место работ готово", msg,
               "work_order", wo.id)
    _changed(db, "work_order", wo.id, wo.station_id)
    domain_event(db, "wagon.uncoupled", msg, payload={"work_order_id": wo.id}, model_time=model_now)
    return msg


def check_overdue(db: Session) -> int:
    """Уведомление о просрочке — однократно на заявку (реальное время)."""
    now = utcnow()
    n = 0
    for wo in db.execute(select(WorkOrder).where(WorkOrder.due_at < now, WorkOrder.status.not_in(
            ["completed", "cancelled", "awaiting_inspection"]))).scalars():
        seen = db.execute(select(func.count()).select_from(WorkEvent).where(
            WorkEvent.entity_id == wo.id, WorkEvent.kind == "overdue")).scalar()
        if seen:
            continue
        _event(db, "work_order", wo.id, None, "overdue", text="Согласованный срок истёк")
        notify(db, [wo.assignee_id] + users_with(db, "work_order.create", wo.station_id), "work_order.overdue",
               f"Просрочено задание № {wo.number}", WO_KIND[wo.kind], "work_order", wo.id)
        _changed(db, "work_order", wo.id, wo.station_id)
        n += 1
    return n


# ================================================================== представления
def _user_name(db: Session, uid: str | None) -> str | None:
    if not uid:
        return None
    u = db.get(User, uid)
    return u.full_name if u else uid


def _timeline(db: Session, et: str, eid: str) -> list[dict]:
    return [{"at": iso(e.created_at), "user": e.user_name, "kind": e.kind, "from": e.from_status, "to": e.to_status,
             "text": e.text} for e in db.execute(select(WorkEvent).where(WorkEvent.entity_type == et, WorkEvent.entity_id == eid)
                                                 .order_by(WorkEvent.id)).scalars()]


def wagon_brief(db: Session, wid: str | None) -> dict | None:
    if not wid:
        return None
    w = db.get(Wagon, wid)
    if not w:
        return None
    t = db.get(Train, w.train_id) if w.train_id else None
    return {"id": w.id, "number": w.number, "kind": w.kind, "loaded": w.loaded, "condition": w.condition,
            "condition_label": WAGON_CONDITION.get(w.condition, w.condition), "position": w.position if t else None,
            "train": {"id": t.id, "number": t.number, "track_id": t.current_track_id, "status": t.status} if t else None,
            "track_id": w.track_id}


def defect_view(db: Session, user: User, r: DefectReport, *, full: bool = True) -> dict:
    now = utcnow()
    out = {"id": r.id, "number": r.number, "status": r.status, "status_label": DEFECT_STATUS[r.status],
           "urgency": r.urgency, "urgency_label": URGENCY[r.urgency], "category": r.category,
           "category_label": CATEGORIES[r.category], "component": r.component, "description": r.description,
           "wagon_number": r.wagon_number_raw, "wagon_linked": bool(r.wagon_id), "wagon": wagon_brief(db, r.wagon_id),
           "train_id": r.train_id, "track_id": r.track_id, "position": r.position, "station_id": r.station_id,
           "author": {"id": r.author_id, "name": _user_name(db, r.author_id)},
           "created_at": iso(r.created_at), "delivered_at": iso(r.created_at), "acknowledged_at": iso(r.acknowledged_at),
           "acknowledged_by": _user_name(db, r.acknowledged_by), "updated_at": iso(r.updated_at), "version": r.version,
           "restriction_active": r.restriction_active, "fault_open": r.fault_open, "decision": r.decision,
           "decision_label": WO_KIND.get(r.decision or "", None), "decision_reason": r.decision_reason,
           "duplicate_of": r.duplicate_of, "client_uuid": r.client_uuid,
           "waiting_min": round((now - aware(r.created_at)).total_seconds() / 60) if r.status in DEFECT_OPEN else None}
    if full:
        out["attachments"] = attachments_of(db, "defect", r.id)
        out["timeline"] = _timeline(db, "defect", r.id)
        out["work_orders"] = [wo_view(db, user, w, full=False) for w in db.execute(
            select(WorkOrder).where(WorkOrder.defect_report_id == r.id).order_by(WorkOrder.number)).scalars()
            if can_view_wo(db, user, w)]
        out["allowed"] = _defect_allowed(db, user, r)
        if r.duplicate_of:
            o = db.get(DefectReport, r.duplicate_of)
            out["duplicate_of_number"] = o.number if o else None
    return out


def _defect_allowed(db: Session, user: User, r: DefectReport) -> dict:
    """Какие действия доступны и, если нет, почему (для подписей у недоступных кнопок)."""
    tri, dec = can(user, "defect.triage"), can(user, "work_order.create")
    opened = r.status in DEFECT_OPEN
    res = {}

    def put(k, ok, why):
        res[k] = None if ok else why
    put("acknowledge", tri and r.status == "submitted", "Подтвердить получение может оператор ПТО или диспетчер, пока сообщение не принято." if not tri else "Получение уже подтверждено.")
    put("review", tri and r.status in ("submitted", "acknowledged", "needs_info"), "Недоступно в текущем статусе." if tri else "Нет права на приём сообщений.")
    put("needs_info", tri and r.status in ("submitted", "acknowledged", "under_review"), "Недоступно в текущем статусе." if tri else "Нет права на приём сообщений.")
    put("reply", r.status == "needs_info" and (r.author_id == user.id or tri), "Ответить можно, когда запрошено уточнение.")
    put("link_wagon", tri and opened, "Привязка — до решения, право оператора ПТО или диспетчера.")
    put("decide", dec and r.status in ("acknowledged", "under_review") and bool(r.wagon_id),
        "Решение принимает станционный диспетчер." if not dec else ("Сначала привяжите сообщение к вагону." if not r.wagon_id else "Решение — после взятия на рассмотрение."))
    put("reject", tri and opened and (dec or not (r.urgency == "critical" or r.restriction_active)),
        "Отклонение критичного сообщения со снятием ограничения — станционный диспетчер." if tri else "Нет права на приём сообщений.")
    put("duplicate", tri and opened, "Недоступно в текущем статусе." if tri else "Нет права на приём сообщений.")
    put("add_work_order", dec and r.status == "accepted", "Дополнительная заявка — по принятому сообщению, станционный диспетчер.")
    return res


def wo_view(db: Session, user: User, w: WorkOrder, *, full: bool = True) -> dict:
    ready, why = _station_ops_state(db, w)
    out = {"id": w.id, "number": w.number, "kind": w.kind, "kind_label": WO_KIND[w.kind], "status": w.status,
           "status_label": WO_STATUS[w.status], "wagon": wagon_brief(db, w.wagon_id), "station_id": w.station_id,
           "defect_report_id": w.defect_report_id, "assignee": {"id": w.assignee_id, "name": _user_name(db, w.assignee_id)} if w.assignee_id else None,
           "brigade_id": w.brigade_id, "due_at": iso(w.due_at), "overdue": bool(w.due_at and aware(w.due_at) < utcnow() and w.status not in ("completed", "cancelled")),
           "version": w.version, "created_at": iso(w.created_at), "updated_at": iso(w.updated_at),
           "replacement_wagon": wagon_brief(db, w.replacement_wagon_id), "place_ready": ready, "place_reason": why or None,
           "hold_reason": w.hold_reason}
    if full:
        r = db.get(DefectReport, w.defect_report_id) if w.defect_report_id else None
        out.update({"actions": w.actions, "report": w.report, "materials": w.materials, "cancel_reason": w.cancel_reason,
                    "started_at": iso(w.started_at), "submitted_at": iso(w.submitted_at), "completed_at": iso(w.completed_at),
                    "executed_by": [_user_name(db, u) for u in w.executed_by or []],
                    "defect": defect_view(db, user, r, full=False) if r and can_view_defect(db, user, r) else None,
                    "attachments": attachments_of(db, "work_order", w.id), "timeline": _timeline(db, "work_order", w.id),
                    "inspections": [{"at": iso(i.created_at), "inspector": _user_name(db, i.inspector_id), "result": i.result,
                                     "comment": i.comment} for i in db.execute(select(WorkInspection).where(
                                         WorkInspection.work_order_id == w.id).order_by(WorkInspection.id)).scalars()],
                    "operations": [{"id": o.id, "note": o.note, "status": o.status, "planned_start": iso(o.planned_start),
                                    "reserved": o.reserved} for o in (db.get(Operation, oid) for oid in w.operation_ids or []) if o],
                    "allowed": _wo_allowed(db, user, w, ready, why)})
    return out


def _wo_allowed(db: Session, user: User, w: WorkOrder, ready: bool, why: str) -> dict:
    mine = w.assignee_id == user.id and can(user, "work_order.execute") and not (w.kind != "inspection" and user.role == "wagon_inspector")
    self_done = user.id == w.assignee_id or user.id in (w.executed_by or [])
    res = {}

    def put(k, ok, msg):
        res[k] = None if ok else msg
    put("start", mine and w.status in ("assigned", "rework") and ready,
        ("Задание назначено другому исполнителю." if not mine else (f"Место работ не готово: {why}." if not ready else "Недоступно в текущем статусе.")))
    put("pause", mine and w.status in ("assigned", "in_progress"), "Приостановить может исполнитель во время работ.")
    put("resume", mine and w.status == "on_hold", "Задание не приостановлено.")
    put("submit", mine and w.status == "in_progress", "Передать результат можно во время выполнения работ.")
    can_insp = can(user, "work_order.inspect")
    put("accept", can_insp and w.status == "awaiting_inspection" and not self_done,
        "Собственную работу принять нельзя — нужен другой проверяющий." if self_done and can_insp else "Приёмку выполняет старший осмотрщик-ремонтник после передачи результата.")
    put("rework", can_insp and w.status == "awaiting_inspection" and not self_done, res["accept"] or "")
    put("assign", can(user, "work_order.assign") and w.status in ("created", "assigned", "on_hold", "rework"), "Назначение — старший осмотрщик или диспетчер.")
    put("cancel", can(user, "work_order.create") and w.status in ("created", "assigned", "in_progress", "on_hold", "rework"), "Отмена — станционный диспетчер.")
    return res


def list_defects(db: Session, user: User, *, scope: str = "auto", status: str | None = None, urgency: str | None = None,
                 track_id: str | None = None, wagon: str | None = None, page: int = 1, page_size: int = 30) -> dict:
    sc = scope_of(db, user)
    q = select(DefectReport)
    if scope == "own" or (scope == "auto" and not can(user, "defect.view_station")):
        if not (can(user, "defect.view_own") or can(user, "defect.view_station")):
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        q = q.where(DefectReport.author_id == user.id)
    else:
        if not can(user, "defect.view_station"):
            raise Forbidden("FORBIDDEN", "Очередь станции недоступна вашей роли.")
        q = q.where(DefectReport.station_id == sc.station_id)
    if status == "open":
        q = q.where(DefectReport.status.in_(DEFECT_OPEN))
    elif status:
        q = q.where(DefectReport.status == status)
    if urgency:
        q = q.where(DefectReport.urgency == urgency)
    if track_id:
        q = q.where(DefectReport.track_id == track_id)
    if wagon:
        q = q.where(DefectReport.wagon_number_raw.like(f"%{wagon.strip()}%"))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar()
    order = [(DefectReport.urgency == "critical").desc(), (DefectReport.urgency == "urgent").desc(), DefectReport.created_at.desc()]
    rows = db.execute(q.order_by(*order).offset((page - 1) * page_size).limit(page_size)).scalars()
    return {"items": [defect_view(db, user, r, full=False) for r in rows], "total": total, "page": page, "page_size": page_size}


def list_work_orders(db: Session, user: User, *, scope: str = "auto", status: str | None = None, kind: str | None = None,
                     assignee: str | None = None, page: int = 1, page_size: int = 30) -> dict:
    sc = scope_of(db, user)
    q = select(WorkOrder)
    station_view = can(user, "defect.view_station") or can(user, "work_order.inspect") or can(user, "work_order.assign")
    if scope == "mine" or (scope == "auto" and not station_view):
        if not can(user, "work_order.execute"):
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        conds = [WorkOrder.assignee_id == user.id]
        if sc.brigade_id:
            conds.append((WorkOrder.brigade_id == sc.brigade_id) & WorkOrder.status.not_in(["completed", "cancelled"])
                         & (WorkOrder.station_id == sc.station_id))
        q = q.where(or_(*conds))
    elif scope == "inspect":
        if not can(user, "work_order.inspect"):
            raise Forbidden("FORBIDDEN", "Контрольный осмотр недоступен вашей роли.")
        q = q.where(WorkOrder.station_id == sc.station_id, WorkOrder.status == "awaiting_inspection")
    else:
        if not station_view:
            raise Forbidden("FORBIDDEN", "Очередь заявок станции недоступна вашей роли.")
        q = q.where(WorkOrder.station_id == sc.station_id)
    if status == "active":
        q = q.where(WorkOrder.status.not_in(["completed", "cancelled"]))
    elif status:
        q = q.where(WorkOrder.status == status)
    if kind:
        q = q.where(WorkOrder.kind == kind)
    if assignee:
        q = q.where(WorkOrder.assignee_id == assignee)
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar()
    rows = db.execute(q.order_by(WorkOrder.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)).scalars()
    return {"items": [wo_view(db, user, w, full=False) for w in rows], "total": total, "page": page, "page_size": page_size}


def executors(db: Session, user: User, kind: str | None = None) -> list[dict]:
    """Исполнители для назначения (в своей станции; для старшего осмотрщика — своей зоны)."""
    sc = scope_of(db, user)
    out = []
    for u in db.execute(select(User).where(User.active.is_(True)).order_by(User.full_name)).scalars():
        if "work_order.execute" not in role_permissions(u.role):
            continue
        usc = scope_of(db, u)
        if usc.station_id != sc.station_id:
            continue
        if not can(user, "work_order.create") and sc.pto_id and usc.pto_id != sc.pto_id:
            continue
        if kind and kind != "inspection" and u.role == "wagon_inspector":
            continue
        active = db.execute(select(func.count()).select_from(WorkOrder).where(
            WorkOrder.assignee_id == u.id, WorkOrder.status.in_(["assigned", "in_progress", "on_hold", "rework"]))).scalar()
        out.append({"id": u.id, "name": u.full_name, "role": u.role, "role_label": role_label(u.role),
                    "brigade_id": usc.brigade_id, "active_tasks": active})
    return out
