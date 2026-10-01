"""API процесса работников (мобильный раздел /mobile и интеграция кабинета диспетчера).

Каждый запрос проверяет право, принадлежность объекта станции/бригаде, назначение и статус на
сервере (services/workflow.py). Чужие объекты неотличимы от несуществующих (404). Изменяющие
команды поддерживают Idempotency-Key и контроль версии записи (expected_version → 409).
"""
from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.audit import audit
from app.core.errors import AppError, Forbidden, NotFound
from app.core.idempotency import run_idempotent
from app.core.permissions import can, role_label, role_permissions
from app.core.security import current_user
from app.core.timeutil import iso, utcnow
from app.db import get_db
from app.models import Attachment, DefectReport, Notification, Station, Track, Train, User, Wagon, WorkOrder
from app.services import workflow as wf

router = APIRouter(prefix="/api/v1", tags=["Работники: дефекты и ремонт"])

MAX_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg": (b"\xff\xd8\xff", ".jpg"), "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
               "image/webp": (b"RIFF", ".webp")}


def _any(user: User, *actions: str):
    if not any(can(user, a) for a in actions):
        raise Forbidden("FORBIDDEN", f"Раздел недоступен роли «{role_label(user.role)}».", details={"actions": actions})


# ------------------------------------------------------------------ схемы
class DefectIn(BaseModel):
    client_uuid: str = Field(min_length=8, max_length=64, description="Локальный UUID сообщения (ключ повтора офлайн-очереди)")
    wagon_id: str | None = None
    wagon_number: str = Field(min_length=1, max_length=16, description="Номер вагона (ввод вручную, если не найден)")
    train_id: str | None = None
    track_id: str | None = None
    position: int | None = Field(default=None, ge=1, le=200)
    category: str
    component: str | None = Field(default=None, max_length=120)
    description: str = Field(min_length=3, max_length=4000)
    urgency: str = "normal"
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)
    location: dict | None = Field(default=None, description="Местоположение — только с согласия пользователя")
    client_created_at: datetime | None = None


class ActionIn(BaseModel):
    expected_version: int | None = None
    reason: str | None = Field(default=None, max_length=4000)
    comment: str | None = Field(default=None, max_length=4000)
    duplicate_of: str | None = None
    wagon_id: str | None = None
    assignee_id: str | None = None
    brigade_id: str | None = None
    report: str | None = Field(default=None, max_length=4000)
    materials: str | None = Field(default=None, max_length=2000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)


class DecisionIn(BaseModel):
    expected_version: int | None = None
    kind: str = Field(description="inspection | repair_in_place | uncoupling_repair | replacement | transfer")
    reason: str | None = Field(default=None, max_length=2000)
    assignee_id: str | None = None
    brigade_id: str | None = None
    due_at: datetime | None = None
    actions: list[str] = Field(default_factory=list, max_length=20)
    replacement_wagon_id: str | None = None


class ReadIn(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=500)
    all: bool = False


def _idem(db, user, key, endpoint, body, fn):
    return run_idempotent(db, user, key, endpoint, body, fn)


# ------------------------------------------------------------------ контекст мобильного раздела
@router.get("/mobile/context", summary="Контекст работника: станция, роль, права, счётчики")
def mobile_context(db: Session = Depends(get_db), user: User = Depends(current_user)):
    _any(user, "mobile.access")
    sc = wf.scope_of(db, user)
    st = db.get(Station, sc.station_id)
    unread = db.execute(select(func.count()).select_from(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None))).scalar()
    my_active = db.execute(select(func.count()).select_from(WorkOrder).where(
        WorkOrder.assignee_id == user.id, WorkOrder.status.in_(["assigned", "in_progress", "on_hold", "rework"]))).scalar()
    needs_info = db.execute(select(func.count()).select_from(DefectReport).where(
        DefectReport.author_id == user.id, DefectReport.status == "needs_info")).scalar()
    inspect = db.execute(select(func.count()).select_from(WorkOrder).where(
        WorkOrder.station_id == sc.station_id, WorkOrder.status == "awaiting_inspection")).scalar() if can(user, "work_order.inspect") else 0
    queue = db.execute(select(func.count()).select_from(DefectReport).where(
        DefectReport.station_id == sc.station_id, DefectReport.status.in_(["submitted", "acknowledged", "under_review"]))).scalar() \
        if can(user, "defect.view_station") else 0
    return {"user": {"id": user.id, "username": user.username, "full_name": user.full_name, "role": user.role,
                     "role_label": role_label(user.role)},
            "permissions": sorted(role_permissions(user.role)),
            "scope": {"station_id": sc.station_id, "station_name": st.name if st else sc.station_id,
                      "pto_id": sc.pto_id, "brigade_id": sc.brigade_id, "explicit": db.get(type(sc), user.id) is not None},
            "counts": {"unread": unread, "my_active": my_active, "needs_info": needs_info, "awaiting_inspection": inspect,
                       "queue": queue},
            "dictionaries": {"categories": wf.CATEGORIES, "urgency": wf.URGENCY, "work_kinds": wf.WO_KIND,
                             "defect_status": wf.DEFECT_STATUS, "work_status": wf.WO_STATUS},
            "server_time": iso(utcnow())}


def _wagon_lookup_allowed(user: User):
    _any(user, "defect.create", "defect.triage", "defect.view_station", "work_order.execute")


def _track_label(db, tid):
    t = db.get(Track, tid) if tid else None
    return (f"Главный путь {t.number}" if t.kind == "main" else f"Путь {t.number}") if t else None


@router.get("/mobile/wagons", summary="Поиск вагона по номеру на своей станции")
def mobile_wagons(q: str = Query(min_length=2, max_length=16), db: Session = Depends(get_db), user: User = Depends(current_user)):
    _wagon_lookup_allowed(user)
    sc = wf.scope_of(db, user)
    if sc.station_id != wf.main_station(db).id:
        return []  # вагоны другой станции не раскрываются
    rows = db.execute(select(Wagon).where(Wagon.number.like(f"%{q.strip()}%")).order_by(Wagon.number).limit(20)).scalars()
    out = []
    for w in rows:
        b = wf.wagon_brief(db, w.id)
        if b["train"]:
            b["train"]["track_label"] = _track_label(db, b["train"]["track_id"])
        b["track_label"] = _track_label(db, w.track_id)
        out.append(b)
    return out


@router.get("/mobile/trains", summary="Составы на станции (для выбора вагона из состава)")
def mobile_trains(db: Session = Depends(get_db), user: User = Depends(current_user)):
    _wagon_lookup_allowed(user)
    sc = wf.scope_of(db, user)
    if sc.station_id != wf.main_station(db).id:
        return []
    out = []
    for t in db.execute(select(Train).where(Train.status.in_(["on_station", "waiting", "approaching"])).order_by(Train.number)).scalars():
        wl = sorted(db.execute(select(Wagon).where(Wagon.train_id == t.id)).scalars(), key=lambda w: w.position)
        out.append({"id": t.id, "number": t.number, "status": t.status, "track_id": t.current_track_id,
                    "track_label": _track_label(db, t.current_track_id),
                    "wagons": [{"id": w.id, "number": w.number, "position": w.position, "kind": w.kind,
                                "condition": w.condition} for w in wl]})
    return out


@router.get("/mobile/tracks", summary="Пути своей станции")
def mobile_tracks(db: Session = Depends(get_db), user: User = Depends(current_user)):
    _wagon_lookup_allowed(user)
    sc = wf.scope_of(db, user)
    return [{"id": t.id, "label": _track_label(db, t.id), "kind": t.kind}
            for t in db.execute(select(Track).where(Track.station_id == sc.station_id).order_by(Track.number)).scalars()]


# ------------------------------------------------------------------ сообщения о дефектах
@router.post("/defect-reports", summary="Сообщение о дефекте (идемпотентно по client_uuid)")
def create_defect(body: DefectIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    data = body.model_dump()
    out, replay = wf.submit_defect(db, user, data)
    db.commit()
    return {**out, "idempotent_replay": replay}


@router.get("/defect-reports", summary="Сообщения: свои или очередь станции (фильтры, пагинация)")
def defects(scope: str = Query("auto", pattern="^(auto|own|station)$"), status: str | None = None, urgency: str | None = None,
            track_id: str | None = None, wagon: str | None = None, page: int = Query(1, ge=1),
            page_size: int = Query(30, ge=1, le=100), db: Session = Depends(get_db), user: User = Depends(current_user)):
    _any(user, "defect.view_own", "defect.view_station")
    return wf.list_defects(db, user, scope=scope, status=status, urgency=urgency, track_id=track_id, wagon=wagon,
                           page=page, page_size=page_size)


@router.get("/defect-reports/{rid}", summary="Карточка сообщения: объект, фото, статус, хронология, работы")
def defect(rid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return wf.defect_view(db, user, wf.get_defect(db, user, rid))


@router.post("/defect-reports/{rid}/actions/{action}", summary="Действие с сообщением: acknowledge, review, needs_info, "
             "reply, reject, duplicate, link_wagon, comment")
def defect_action(rid: str, action: str, body: ActionIn, db: Session = Depends(get_db), user: User = Depends(current_user),
                  idempotency_key: str | None = Header(default=None)):
    data = body.model_dump()
    return _idem(db, user, idempotency_key, f"defect.{action}:{rid}", data, lambda: wf.defect_action(db, user, rid, action, data))


@router.post("/defect-reports/{rid}/decision", summary="Решение по сообщению: принять и создать заявку на работы")
def defect_decision(rid: str, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(current_user),
                    idempotency_key: str | None = Header(default=None)):
    data = body.model_dump()
    return _idem(db, user, idempotency_key, f"defect.decision:{rid}", data, lambda: wf.decide(db, user, rid, data))


@router.post("/defect-reports/{rid}/work-orders", summary="Дополнительная заявка по принятому сообщению")
def defect_add_wo(rid: str, body: DecisionIn, db: Session = Depends(get_db), user: User = Depends(current_user),
                  idempotency_key: str | None = Header(default=None)):
    data = body.model_dump()
    return _idem(db, user, idempotency_key, f"defect.add_wo:{rid}", data, lambda: wf.add_work_order(db, user, rid, data))


# ------------------------------------------------------------------ заявки на работы
@router.get("/work-orders", summary="Заявки: мои / бригады, ожидающие осмотра, очередь станции")
def work_orders(scope: str = Query("auto", pattern="^(auto|mine|inspect|station)$"), status: str | None = None,
                kind: str | None = None, assignee: str | None = None, page: int = Query(1, ge=1),
                page_size: int = Query(30, ge=1, le=100), db: Session = Depends(get_db), user: User = Depends(current_user)):
    _any(user, "work_order.execute", "work_order.inspect", "work_order.assign", "defect.view_station")
    return wf.list_work_orders(db, user, scope=scope, status=status, kind=kind, assignee=assignee, page=page, page_size=page_size)


@router.get("/work-orders/executors", summary="Исполнители для назначения")
def wo_executors(kind: str | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _any(user, "work_order.assign", "work_order.create")
    return wf.executors(db, user, kind)


@router.get("/work-orders/{wid}", summary="Карточка заявки: работы, ход, отчёт, контрольный осмотр")
def work_order(wid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return wf.wo_view(db, user, wf.get_wo(db, user, wid))


@router.post("/work-orders/{wid}/actions/{action}", summary="Действие с заявкой: assign, start, pause, resume, submit, "
             "accept, rework, cancel, comment")
def wo_action(wid: str, action: str, body: ActionIn, db: Session = Depends(get_db), user: User = Depends(current_user),
              idempotency_key: str | None = Header(default=None)):
    data = body.model_dump()
    return _idem(db, user, idempotency_key, f"wo.{action}:{wid}", data, lambda: wf.wo_action(db, user, wid, action, data))


@router.get("/wagons/{wagon_id}/replacement-candidates", summary="Подбор исправного вагона для замены")
def replacement_candidates(wagon_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _any(user, "wagon_replacement.approve")
    w = db.get(Wagon, wagon_id)
    if not w:
        raise NotFound("WAGON_NOT_FOUND", "Вагон не найден.")
    return wf.replacement_candidates(db, w)


# ------------------------------------------------------------------ вложения
def _dir() -> str:
    d = get_settings().attachments_dir
    os.makedirs(d, exist_ok=True)
    return d


def _attachment_visible(db, user: User, a: Attachment) -> bool:
    if a.uploader_id == user.id:
        return True
    if a.owner_type == "defect":
        r = db.get(DefectReport, a.owner_id)
        return bool(r and wf.can_view_defect(db, user, r))
    if a.owner_type == "work_order":
        w = db.get(WorkOrder, a.owner_id)
        return bool(w and wf.can_view_wo(db, user, w))
    return False


@router.post("/attachments", summary="Загрузить фотографию (тело — файл; идемпотентно по client_uuid)")
async def upload(request: Request, client_uuid: str = Query(min_length=8, max_length=64),
                 filename: str = Query("photo.jpg", max_length=120), db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    _any(user, "defect.create", "defect.triage", "work_order.execute", "work_order.inspect")
    old = db.execute(select(Attachment).where(Attachment.uploader_id == user.id, Attachment.client_uuid == client_uuid)).scalar_one_or_none()
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in IMAGE_TYPES:
        raise AppError("BAD_FILE_TYPE", "Можно прикладывать только фотографии JPEG, PNG или WebP.", status=415)
    data = b""
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_BYTES:
            raise AppError("FILE_TOO_LARGE", "Файл больше 8 МБ.", status=413, hint="Приложение уменьшает фото перед отправкой.")
    magic, ext = IMAGE_TYPES[ctype]
    if not data.startswith(magic) or (ctype == "image/webp" and data[8:12] != b"WEBP"):
        raise AppError("BAD_FILE_CONTENT", "Содержимое файла не соответствует типу изображения.", status=415)
    digest = hashlib.sha256(data).hexdigest()
    if old:
        if old.sha256 != digest:
            raise AppError("IDEMPOTENCY_KEY_REUSED", "Этот идентификатор вложения уже использован для другого файла.", status=409)
        return {"id": old.id, "idempotent_replay": True, "size": old.size}
    aid = f"AT-{uuid.uuid4().hex[:12]}"
    storage = f"{uuid.uuid4().hex}{ext}"  # безопасное имя: исходное имя не используется в пути
    path = os.path.join(_dir(), storage)
    with open(path, "wb") as f:
        f.write(data)
    safe_name = "".join(ch for ch in os.path.basename(filename) if ch.isalnum() or ch in "._- ")[:120] or "photo"
    db.add(Attachment(id=aid, client_uuid=client_uuid, uploader_id=user.id, original_name=safe_name, content_type=ctype,
                      size=len(data), sha256=digest, storage_name=storage, created_at=utcnow()))
    try:
        db.commit()
    except Exception:
        os.remove(path)
        raise
    return {"id": aid, "idempotent_replay": False, "size": len(data)}


@router.get("/attachments/{aid}", summary="Скачать вложение (только при доступе к записи)")
def download(aid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    a = db.get(Attachment, aid)
    if not a or not _attachment_visible(db, user, a):
        raise NotFound("ATTACHMENT_NOT_FOUND", "Вложение не найдено.")
    path = os.path.join(_dir(), a.storage_name)
    if not os.path.exists(path):
        raise NotFound("ATTACHMENT_MISSING", "Файл вложения недоступен.")
    return FileResponse(path, media_type=a.content_type, headers={"Cache-Control": "private, no-store",
                                                                 "X-Content-Type-Options": "nosniff"})


@router.delete("/attachments/{aid}", summary="Удалить непривязанное вложение")
def delete_attachment(aid: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    a = db.get(Attachment, aid)
    if not a or a.uploader_id != user.id:
        raise NotFound("ATTACHMENT_NOT_FOUND", "Вложение не найдено.")
    if a.owner_id:
        raise AppError("ATTACHMENT_LINKED", "Вложение уже прикреплено к записи и хранится в её истории.", status=409)
    try:
        os.remove(os.path.join(_dir(), a.storage_name))
    except FileNotFoundError:
        pass
    db.delete(a)
    db.commit()
    return {"ok": True}


def cleanup_attachments(db: Session, hours: int = 24) -> int:
    """Удаление незавершённых загрузок (не привязанных к записи дольше суток)."""
    n = 0
    for a in db.execute(select(Attachment).where(Attachment.owner_id.is_(None),
                                                 Attachment.created_at < utcnow() - timedelta(hours=hours))).scalars():
        try:
            os.remove(os.path.join(_dir(), a.storage_name))
        except FileNotFoundError:
            pass
        db.delete(a)
        n += 1
    return n


# ------------------------------------------------------------------ уведомления
@router.get("/notifications", summary="Уведомления пользователя")
def notifications(unread: bool = False, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    q = select(Notification).where(Notification.user_id == user.id)
    if unread:
        q = q.where(Notification.read_at.is_(None))
    rows = db.execute(q.order_by(Notification.id.desc()).limit(limit)).scalars()
    total_unread = db.execute(select(func.count()).select_from(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None))).scalar()
    return {"unread": total_unread, "items": [{"id": n.id, "kind": n.kind, "title": n.title, "body": n.body,
                                               "entity_type": n.entity_type, "entity_id": n.entity_id,
                                               "created_at": iso(n.created_at), "read": n.read_at is not None} for n in rows]}


@router.post("/notifications/read", summary="Отметить уведомления прочитанными")
def notifications_read(body: ReadIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = select(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
    if not body.all:
        q = q.where(Notification.id.in_(body.ids or [-1]))
    n = 0
    for x in db.execute(q).scalars():
        x.read_at = utcnow()
        n += 1
    db.commit()
    return {"marked": n}
