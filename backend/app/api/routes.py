"""HTTP API «Цифровой станции» (v1). Все изменяющие операции проверяют права на сервере,
поддерживают заголовок Idempotency-Key и записываются в аудит."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.api import schemas as S
from app.config import get_settings
from app.core import metrics
from app.core.audit import audit, domain_event
from app.core.errors import AppError, Conflict, Forbidden, NotFound, Unauthorized
from app.core.idempotency import run_idempotent
from app.core.permissions import ROLES, all_roles, can, invalidate, matrix, require, role_label, role_permissions
from app.core.security import current_user, issue_token, verify_password
from app.core.timeutil import aware, iso, utcnow
from app.db import get_db
from app.domain.statuses import LABELS
from app.iot.quality import STATE_LABELS, device_statuses, track_data_states
from app.models import (
    AuditEvent, CapacityRule, Device, DomainEvent, IndexConfig, IndexSnapshot, ManualOverride, Operation, Plan,
    PlanVersion, Recommendation, Reservation, SimState, StateDelta, StateSnapshot, Station, TelemetryEvent,
    TelemetryReject, TopologyNode, Track, TrackConnection, Train, TransferRequest, User, Wagon, Zone, Park,
)
from app.services import requests as reqsvc
from app.services.model import KIND_LABEL, StationModel
from app.services.versioning import bump

router = APIRouter(prefix="/api/v1")


# ------------------------------------------------------------------ служебные
@router.get("/health", tags=["Служебные"], summary="Состояние сервиса")
def health(db: Session = Depends(get_db)):
    from app.iot.runtime import ingest_service
    from app.services.hub import hub
    db.execute(select(1))
    return {"status": "ok", "db": "ok", "mqtt": ingest_service.status(), "ws_clients": len(hub.clients),
            "view_version": hub.version, "time": iso(utcnow())}


@router.post("/auth/login", tags=["Доступ"], summary="Вход: выдаёт токен доступа")
def login(body: S.LoginIn, db: Session = Depends(get_db)):
    u = db.execute(select(User).where(User.username == body.username)).scalar_one_or_none()
    if not u or not verify_password(body.password, u.password_hash) or not u.active:
        raise Unauthorized("BAD_CREDENTIALS", "Неверное имя пользователя или пароль.")
    return {"token": issue_token(u), "user": _user(u)}


@router.get("/auth/me", tags=["Доступ"], summary="Текущий пользователь и его права")
def me(user: User = Depends(current_user)):
    return {**_user(user), "permissions": sorted(role_permissions(user.role))}


@router.get("/auth/demo-users", tags=["Доступ"], summary="Демонстрационные учётные записи (только для стенда)")
def demo_users(db: Session = Depends(get_db)):
    from app.sim.seed import DEMO_USERS
    demo = {un for _, un, _, _ in DEMO_USERS}  # на экране входа — только демо-учётки, не созданные администратором
    return [{"username": u.username, "full_name": u.full_name, "role": u.role, "role_label": role_label(u.role)}
            for u in db.execute(select(User).where(User.username.in_(demo), User.active.is_(True))
                                .order_by(User.id)).scalars()]


@router.post("/auth/docs-session", tags=["Доступ"], summary="Открыть доступ к Swagger в браузере (только администратор)")
def docs_session(request: Request, user: User = Depends(require("api.docs"))):
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True, "url": "/docs"})
    # HttpOnly: токен не доступен скриптам страницы; SameSite=Strict: не отправляется с чужих сайтов
    resp.set_cookie("ds_docs", issue_token(user), max_age=3600, httponly=True, samesite="strict", path="/")
    return resp


@router.get("/permissions", tags=["Доступ"], summary="Матрица прав (только администратор)")
def permissions(_: User = Depends(require("users.manage"))):
    return {"roles": all_roles(), "system_roles": list(ROLES), "matrix": matrix(),
            "note": "Ролевая модель — допущение MVP; требует проверки полномочий профильным специалистом."}


def _user(u: User) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role, "role_label": role_label(u.role)}


# ------------------------------------------------------------------ состояние и топология
@router.get("/state", tags=["Состояние"], summary="Полное текущее состояние станции (то же, что первый снимок WebSocket)")
def state(_: User = Depends(require("state.view"))):
    from app.services.hub import hub
    if hub.state is None:
        raise AppError("STATE_NOT_READY", "Состояние ещё формируется. Повторите через секунду.", status=503)
    return {"version": hub.version, "state": hub.state}


@router.get("/topology", tags=["Состояние"], summary="Топология и геометрия схемы (общие идентификаторы для 2D и 3D)")
def topology(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    st = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
    nodes = [dict(id=n.id, kind=n.kind, name=n.name, x=n.x, y=n.y, side=n.side) for n in db.execute(select(TopologyNode)).scalars()]
    tracks = [dict(id=t.id, number=t.number, name=t.name, kind=t.kind, park_id=t.park_id, useful_length_m=t.useful_length_m,
                   allowed_train_kinds=t.allowed_train_kinds, from_node=t.from_node, to_node=t.to_node, zone_id=t.zone_id,
                   points=t.points) for t in db.execute(select(Track)).scalars()]
    conns = [dict(id=c.id, from_node=c.from_node, to_node=c.to_node, kind=c.kind, track_id=c.track_id, length_m=c.length_m,
                  points=c.points) for c in db.execute(select(TrackConnection)).scalars()]
    zones = [dict(id=z.id, name=z.name, kind=z.kind, track_ids=z.track_ids, x=z.x, y=z.y, params=z.params)
             for z in db.execute(select(Zone)).scalars()]
    parks = [dict(id=p.id, name=p.name, kind=p.kind) for p in db.execute(select(Park)).scalars()]
    devices = [dict(id=d.id, name=d.name, kind=d.kind, object_id=d.object_id, x=d.x, y=d.y, source_mode=d.source_mode)
               for d in db.execute(select(Device)).scalars()]
    xs = [p[0] for t in tracks for p in t["points"]] + [n["x"] for n in nodes]
    ys = [p[1] for t in tracks for p in t["points"]] + [n["y"] for n in nodes] + [z["y"] for z in zones]
    return {"station": {"id": st.id, "name": st.name, "timezone": st.timezone, "is_demo": st.is_demo,
                        "note": st.config["station"].get("note"), "config_id": st.config.get("config_id")},
            "nodes": nodes, "tracks": tracks, "connections": conns, "zones": zones, "parks": parks, "devices": devices,
            "geometry": (st.config or {}).get("derived") or {"schema_scale_u_per_m": 0.8},
            "layout": {k: st.config["layout"].get(k) for k in ("lane_gap", "x_entry_west", "x_entry_east")},
            "bounds": {"min_x": min(xs) - 40, "max_x": max(xs) + 40, "min_y": min(ys) - 50, "max_y": max(ys) + 50}}


@router.get("/network", tags=["Состояние"], summary="Железнодорожная сеть: станции, перегоны, пути перегонов (метры ENU)")
def network(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    from app.domain.network import network_for_station_cfg
    st = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
    try:
        return network_for_station_cfg(st.config or {})
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@router.get("/stations", tags=["Состояние"], summary="Основная и соседние станции (упрощённое состояние)")
def stations(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    out = []
    for s in db.execute(select(Station)).scalars():
        c = s.config or {}
        out.append({"id": s.id, "name": s.name, "kind": s.kind, "is_demo": s.is_demo,
                    "side": c.get("side"), "travel_min": c.get("travel_min"),
                    "max_train_length_m": c.get("max_train_length_m"), "receiving_tracks": c.get("receiving_tracks"),
                    "locomotives_available": c.get("locomotives_available"), "accepts": c.get("accepts"),
                    "occupancy": c.get("occupancy")})
    return out


@router.get("/schedule", tags=["Расписание"], summary="Расписание поездов с прогнозом")
def schedule(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    model = StationModel(db)
    from app.services.forecast import departure_delays, forecast
    fc = forecast(model)
    delays = departure_delays(model, fc)
    rows = []
    for t in sorted(model.trains.values(), key=lambda t: aware(t.scheduled_arrival or t.scheduled_departure or model.now)):
        ops = model.ops_by_train.get(t.id, [])
        arr = next((o for o in ops if o.kind == "arrival"), None)
        dep = next((o for o in ops if o.kind == "departure"), None)
        rows.append({"id": t.id, "number": t.number, "kind": t.kind, "priority": t.priority, "status": t.status,
                     "status_label": LABELS["train"].get(t.status, t.status), "wagons": t.wagons_count,
                     "origin": t.origin_station_id, "destination": t.destination_station_id,
                     "scheduled_arrival": iso(t.scheduled_arrival), "forecast_arrival": iso(fc[arr.id][0]) if arr and arr.id in fc else None,
                     "scheduled_departure": iso(t.scheduled_departure),
                     "forecast_departure": iso(fc[dep.id][0]) if dep and dep.id in fc else None,
                     "track_id": (arr.track_id if arr else None), "track_label": model.track_label(arr.track_id) if arr else None,
                     "delay_min": round(delays.get(t.id, 0)), "transfer_request_id": t.transfer_request_id})
    return {"model_time": iso(model.now), "rows": rows}


@router.get("/trains/{train_id}", tags=["Расписание"], summary="Карточка поезда: операции, вагоны, резервы")
def train_card(train_id: str, db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    model = StationModel(db)
    t = model.trains.get(train_id)
    if not t:
        raise NotFound("TRAIN_NOT_FOUND", "Поезд не найден.")
    ops = model.ops_by_train.get(t.id, [])
    res = [r for r in model.reservations if r.train_id == t.id]
    return {"train": {"id": t.id, "number": t.number, "kind": t.kind, "priority": t.priority, "status": t.status,
                      "status_label": LABELS["train"].get(t.status), "wagons": t.wagons_count,
                      "length_m": model.train_length(t), "track_id": t.current_track_id, "cargo": t.cargo,
                      "origin": t.origin_station_id, "destination": t.destination_station_id},
            "operations": [{"id": o.id, "kind": o.kind, "kind_label": KIND_LABEL.get(o.kind), "status": o.status,
                            "status_label": LABELS["operation"].get(o.status), "track": model.track_label(o.track_id),
                            "track_id": o.track_id, "planned_start": iso(o.planned_start), "planned_end": iso(o.planned_end),
                            "forecast_start": iso(o.forecast_start), "forecast_end": iso(o.forecast_end),
                            "actual_start": iso(o.actual_start), "actual_end": iso(o.actual_end),
                            "resources": [model.resources[r].name for r in (o.resource_ids or []) if r in model.resources],
                            "route": o.route_nodes, "reserved": o.reserved, "note": o.note} for o in ops],
            "wagons": [{"id": w.id, "number": w.number, "kind": w.kind, "length_m": w.length_m, "condition": w.condition,
                        "loaded": w.loaded, "last_checkpoint": w.last_checkpoint, "last_seen_at": iso(w.last_seen_at)}
                       for w in sorted(model.wagons_by_train.get(t.id, []), key=lambda w: w.position)],
            "reservations": [{"key": r.resource_key, "start": iso(r.start_at), "end": iso(r.end_at), "purpose": r.purpose}
                             for r in res]}


@router.get("/tracks/{track_id}", tags=["Состояние"], summary="Карточка пути: данные датчика, резервы, операции")
def track_card(track_id: str, db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    model = StationModel(db)
    t = model.track_rows.get(track_id)
    if not t:
        raise NotFound("TRACK_NOT_FOUND", "Путь не найден.")
    res = sorted((r for r in model.reservations if r.resource_key == f"track:{track_id}" and aware(r.end_at) > model.now - timedelta(hours=1)),
                 key=lambda r: r.start_at)
    ev = list(db.execute(select(TelemetryEvent).where(TelemetryEvent.object_id == track_id)
                         .order_by(desc(TelemetryEvent.id)).limit(20)).scalars())
    return {"track": {"id": t.id, "number": t.number, "label": model.track_label(t.id), "kind": t.kind,
                      "useful_length_m": t.useful_length_m, "allowed_train_kinds": t.allowed_train_kinds, "zone_id": t.zone_id},
            "data_state": model.data_states.get(track_id),
            "reservations": [{"start": iso(r.start_at), "end": iso(r.end_at), "purpose": r.purpose, "train_id": r.train_id,
                              "request_id": r.request_id} for r in res],
            "maintenance": [{"start": iso(m.start_at), "end": iso(m.end_at), "reason": m.reason}
                            for m in model.maintenance if m.object_id == track_id],
            "telemetry": [{"observed_at": iso(e.observed_at), "received_at": iso(e.received_at), "payload": e.payload,
                           "disposition": e.disposition, "device_id": e.device_id} for e in ev]}


@router.get("/capacity", tags=["Загрузка"], summary="Ограничения станции (единицы, период, источник, правило) и загрузка")
def capacity(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    from app.services.checker import _month_bounds, plan_figures
    model = StationModel(db)
    ms, me = _month_bounds(model.now)
    pf = plan_figures(model, ms, me)
    rules = [{"code": r.code, "name": r.name, "category": r.category, "unit": r.unit, "period": r.period, "source": r.source,
              "rule": r.rule_text, "value": r.value, "policy": r.policy, "active": r.active}
             for r in db.execute(select(CapacityRule)).scalars()]
    hours = []
    rd = [tid for tid, t in model.track_rows.items() if t.kind in ("receiving_departure", "sorting", "cargo")]
    for h in range(12):
        a = model.now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=h)
        b = a + timedelta(hours=1)
        row = {"hour": iso(a), "tracks": {}}
        for tid in rd:
            busy = 0.0
            for r in model.reservations:
                if r.resource_key == f"track:{tid}":
                    s, e = max(aware(r.start_at), a), min(aware(r.end_at), b)
                    if e > s:
                        busy += (e - s).total_seconds() / 60
            row["tracks"][tid] = round(min(60, busy))
        hours.append(row)
    res_load = {}
    for rid, r in model.resources.items():
        busy = 0.0
        for rr in model.reservations:
            if rr.resource_key == f"res:{rid}":
                s, e = max(aware(rr.start_at), model.now), min(aware(rr.end_at), model.now + timedelta(hours=8))
                if e > s:
                    busy += (e - s).total_seconds() / 60
        res_load[rid] = {"name": r.name, "kind": r.kind, "busy_min_8h": round(busy)}
    return {"model_time": iso(model.now), "plan": {**pf, "month": ms.strftime("%m.%Y"),
                                                   "policy_label": "Жёсткая квота" if pf["policy"] == "hard_quota" else "Целевой показатель"},
            "rules": rules, "hourly": hours, "track_labels": {tid: model.track_label(tid) for tid in rd},
            "resources": res_load}


# ------------------------------------------------------------------ заявки
@router.get("/requests", tags=["Заявки"], summary="Заявки между станциями")
def list_requests(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    model = StationModel(db, with_reservations=False, data_states={})
    return [reqsvc.serialize(r, model) for r in db.execute(select(TransferRequest).order_by(desc(TransferRequest.created_at))).scalars()]


@router.get("/requests/{rid}", tags=["Заявки"], summary="Заявка и результат последней проверки")
def get_request(rid: str, db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    r = db.get(TransferRequest, rid)
    if not r:
        raise NotFound("REQUEST_NOT_FOUND", "Заявка не найдена.")
    model = StationModel(db, with_reservations=False, data_states={})
    return reqsvc.serialize(r, model)


@router.post("/requests", tags=["Заявки"], summary="Создать заявку на отправление состава на станцию")
def create_request(body: S.RequestCreate, db: Session = Depends(get_db), user: User = Depends(require("request.create")),
                   idempotency_key: str | None = Header(default=None)):
    return run_idempotent(db, user, idempotency_key, "POST /requests", body.model_dump(mode="json"),
                          lambda: reqsvc.serialize(reqsvc.create_request(db, user, body.model_dump())))


@router.post("/requests/{rid}/check", tags=["Заявки"], summary="Проверить приём: все ограничения, окно, альтернативы")
def check(rid: str, db: Session = Depends(get_db), user: User = Depends(require("request.check"))):
    t0 = time.perf_counter()
    out = reqsvc.run_check(db, user, rid)
    db.commit()
    metrics.observe("api_ms", (time.perf_counter() - t0) * 1000)
    return out


@router.post("/requests/{rid}/confirm", tags=["Заявки"], summary="Подтвердить заявку (повторная проверка и резервирование)")
def confirm(rid: str, body: S.ConfirmIn, db: Session = Depends(get_db), user: User = Depends(require("request.confirm")),
            idempotency_key: str | None = Header(default=None)):
    return run_idempotent(db, user, idempotency_key, f"POST /requests/{rid}/confirm", body.model_dump(),
                          lambda: reqsvc.confirm(db, user, rid, body.acknowledge_warnings))


@router.post("/requests/{rid}/cancel", tags=["Заявки"], summary="Отменить заявку и освободить резервы")
def cancel(rid: str, body: S.ReasonIn, db: Session = Depends(get_db), user: User = Depends(require("request.cancel")),
           idempotency_key: str | None = Header(default=None)):
    return run_idempotent(db, user, idempotency_key, f"POST /requests/{rid}/cancel", body.model_dump(),
                          lambda: reqsvc.cancel(db, user, rid, body.reason))


@router.post("/requests/{rid}/reject", tags=["Заявки"], summary="Отказать по заявке")
def reject(rid: str, body: S.ReasonIn, db: Session = Depends(get_db), user: User = Depends(require("request.reject")),
           idempotency_key: str | None = Header(default=None)):
    return run_idempotent(db, user, idempotency_key, f"POST /requests/{rid}/reject", body.model_dump(),
                          lambda: reqsvc.reject(db, user, rid, body.reason))


@router.post("/requests/{rid}/reschedule", tags=["Заявки"], summary="Перенести отправление (с перепроверкой)")
def reschedule(rid: str, body: S.RescheduleIn, db: Session = Depends(get_db),
               user: User = Depends(require("request.reschedule")), idempotency_key: str | None = Header(default=None)):
    return run_idempotent(db, user, idempotency_key, f"POST /requests/{rid}/reschedule", body.model_dump(mode="json"),
                          lambda: reqsvc.reschedule(db, user, rid, body.departure))


@router.post("/requests/{rid}/apply-alternative", tags=["Заявки"], summary="Применить проверенную альтернативу")
def apply_alternative(rid: str, body: S.AlternativeIn, db: Session = Depends(get_db), user: User = Depends(current_user),
                      idempotency_key: str | None = Header(default=None)):
    need = {"postpone": "request.reschedule", "other_station": "request.cancel", "split": "request.create",
            "reorder": "operation.reschedule"}.get(body.action.get("type"))
    if not need:
        raise AppError("UNKNOWN_ALTERNATIVE", "Неизвестный вид альтернативы.")
    require(need)(user)
    return run_idempotent(db, user, idempotency_key, f"POST /requests/{rid}/apply-alternative", body.action,
                          lambda: reqsvc.apply_alternative(db, user, rid, body.action))


# ------------------------------------------------------------------ планы, операции, рекомендации
@router.post("/plans/compute", tags=["Планирование"], summary="Рассчитать предложение плана (CP-SAT, лимит времени)")
def compute_plan(db: Session = Depends(get_db), user: User = Depends(require("plan.compute"))):
    from app.services.hub import hub
    res = hub.replanner.run_once(f"manual:{user.username}", force=True)
    if res is None:
        raise Conflict("PLAN_DISCARDED", "Состояние изменилось во время расчёта — результат отброшен как устаревший.",
                       hint="Повторите расчёт.")
    return plan_details(res["plan_id"], db, user)


@router.get("/plans", tags=["Планирование"], summary="Версии планов")
def list_plans(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    cur = db.get(SimState, 1).plan_state_version
    return [{"id": p.id, "created_at": iso(p.created_at), "model_time": iso(p.model_time), "trigger": p.trigger,
             "status": "stale" if p.status == "proposed" and p.base_state_version != cur else p.status,
             "solver": p.solver, "solver_status": p.solver_status, "solve_ms": p.solve_ms,
             "changes": len(p.changes), "summary": p.summary}
            for p in db.execute(select(PlanVersion).order_by(desc(PlanVersion.id)).limit(30)).scalars()]


@router.get("/plans/{plan_id}", tags=["Планирование"], summary="Предложенный план: изменения, эффект, допущения")
def plan_details(plan_id: int, db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    p = db.get(PlanVersion, plan_id)
    if not p:
        raise NotFound("PLAN_NOT_FOUND", "План не найден.")
    cur = db.get(SimState, 1).plan_state_version
    from app.services.planner import STATUS
    return {"id": p.id, "status": "stale" if p.status == "proposed" and p.base_state_version != cur else p.status,
            "base_state_version": p.base_state_version, "current_state_version": cur, "solver": p.solver,
            "solver_status": p.solver_status, "solver_status_label": STATUS.get(p.solver_status, p.solver_status),
            "solve_ms": p.solve_ms, "created_at": iso(p.created_at), "model_time": iso(p.model_time),
            "trigger": p.trigger, "summary": p.summary, "changes": p.changes, "assignments": p.assignments,
            "assumptions": p.assumptions}


@router.post("/plans/{plan_id}/apply", tags=["Планирование"], summary="Применить план (повторная проверка на сервере)")
def apply_plan(plan_id: int, db: Session = Depends(get_db), user: User = Depends(require("plan.apply")),
               idempotency_key: str | None = Header(default=None)):
    from app.services.plans import apply_plan as ap
    return run_idempotent(db, user, idempotency_key, f"POST /plans/{plan_id}/apply", {}, lambda: ap(db, user, plan_id))


@router.post("/operations/{op_id}/reschedule", tags=["Планирование"], summary="Ручной перенос операции (с проверкой ограничений)")
def reschedule_op(op_id: str, body: S.OperationRescheduleIn, db: Session = Depends(get_db),
                  user: User = Depends(require("operation.reschedule")), idempotency_key: str | None = Header(default=None)):
    from app.services.plans import reschedule_operation
    return run_idempotent(db, user, idempotency_key, f"POST /operations/{op_id}/reschedule", body.model_dump(mode="json"),
                          lambda: reschedule_operation(db, user, op_id, body.start, body.track_id, body.reason))


@router.get("/recommendations", tags=["Планирование"], summary="Рекомендации")
def recommendations(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    cur = db.get(SimState, 1).plan_state_version
    return [{"id": r.id, "kind": r.kind, "title": r.title, "reason": r.reason, "affected": r.affected, "action": r.action,
             "effect": r.effect, "computed_at": iso(r.computed_at), "computed_real_at": iso(r.computed_real_at),
             "based_on_version": r.based_on_version,
             "status": "stale" if r.status == "active" and r.based_on_version != cur else r.status}
            for r in db.execute(select(Recommendation).order_by(desc(Recommendation.computed_real_at)).limit(30)).scalars()]


@router.post("/recommendations/{rec_id}/apply", tags=["Планирование"], summary="Применить рекомендацию (только актуальную)")
def apply_recommendation(rec_id: str, db: Session = Depends(get_db), user: User = Depends(require("recommendation.apply")),
                         idempotency_key: str | None = Header(default=None)):
    rec = db.get(Recommendation, rec_id)
    if not rec:
        raise NotFound("RECOMMENDATION_NOT_FOUND", "Рекомендация не найдена.")
    cur = db.get(SimState, 1).plan_state_version
    if rec.status != "active" or rec.based_on_version != cur:
        raise Conflict("RECOMMENDATION_STALE",
                       f"Рекомендация устарела: рассчитана для версии состояния {rec.based_on_version}, текущая — {cur}. "
                       f"Применение без перерасчёта запрещено.", hint="Нажмите «Пересчитать».",
                       details={"based_on_version": rec.based_on_version, "current_version": cur})
    if rec.action.get("type") == "apply_plan":
        if not can(user, "plan.apply"):
            raise Forbidden("FORBIDDEN", "Применение плана доступно станционному диспетчеру.",
                            hint="Войдите как станционный диспетчер.")
        from app.services.plans import apply_plan as ap

        def act():
            out = ap(db, user, rec.action["plan_id"])
            rec.status = "applied"
            return out
        return run_idempotent(db, user, idempotency_key, f"POST /recommendations/{rec_id}/apply", {}, act)
    raise AppError("UNSUPPORTED_RECOMMENDATION", "Этот вид рекомендации применяется через связанный раздел.")


# ------------------------------------------------------------------ инциденты
@router.get("/incidents", tags=["Инциденты"], summary="Инциденты")
def incidents(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    from app.models import Incident
    return [{"id": i.id, "kind": i.kind, "title": i.title, "description": i.description, "object_type": i.object_type,
             "object_id": i.object_id, "start": iso(i.start_at), "end": iso(i.end_at), "status": i.status,
             "status_label": LABELS["incident"][i.status], "created_at": iso(i.created_at)}
            for i in db.execute(select(Incident).order_by(desc(Incident.created_at))).scalars()]


@router.post("/incidents", tags=["Инциденты"], summary="Зарегистрировать инцидент")
def create_incident(body: S.IncidentIn, db: Session = Depends(get_db), user: User = Depends(require("incident.manage")),
                    idempotency_key: str | None = Header(default=None)):
    from app.services.incidents import create_incident as ci

    def act():
        inc = ci(db, user, body.kind, body.object_id, duration_min=body.duration_min, extra_min=body.extra_min,
                 title=body.title, description=body.description)
        return {"id": inc.id, "title": inc.title}
    return run_idempotent(db, user, idempotency_key, "POST /incidents", body.model_dump(), act)


@router.post("/incidents/{inc_id}/resolve", tags=["Инциденты"], summary="Отметить инцидент устранённым")
def resolve_incident(inc_id: str, body: S.ReasonIn, db: Session = Depends(get_db), user: User = Depends(require("incident.manage")),
                     idempotency_key: str | None = Header(default=None)):
    from app.services.incidents import resolve_incident as ri
    return run_idempotent(db, user, idempotency_key, f"POST /incidents/{inc_id}/resolve", body.model_dump(),
                          lambda: {"id": ri(db, user, inc_id, body.reason).id})


# ------------------------------------------------------------------ IoT
@router.get("/devices", tags=["IoT"], summary="Реестр устройств: подключение и качество данных (раздельно)")
def devices(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    from app.iot.runtime import ingest_service
    return {"devices": device_statuses(db, utcnow()), "ingest": ingest_service.status(), "labels": STATE_LABELS}


@router.get("/devices/{device_id}", tags=["IoT"], summary="История измерений и ошибок устройства")
def device_history(device_id: str, limit: int = Query(100, le=500), db: Session = Depends(get_db),
                   _: User = Depends(require("state.view"))):
    d = db.get(Device, device_id)
    if not d:
        raise NotFound("DEVICE_NOT_FOUND", "Устройство не найдено.")
    ev = db.execute(select(TelemetryEvent).where(TelemetryEvent.device_id == device_id).order_by(desc(TelemetryEvent.id)).limit(limit)).scalars()
    rj = db.execute(select(TelemetryReject).where(TelemetryReject.device_id == device_id).order_by(desc(TelemetryReject.id)).limit(50)).scalars()
    return {"device": {"id": d.id, "name": d.name, "kind": d.kind, "object_id": d.object_id, "period_s": d.period_s,
                       "stale_after_s": d.stale_after_s, "allowed_event_types": d.allowed_event_types, "status": d.status,
                       "source_mode": d.source_mode, "last_seen_at": iso(d.last_seen_at),
                       "last_heartbeat_at": iso(d.last_heartbeat_at), "last_seq": d.last_seq, "last_boot_id": d.last_boot_id,
                       "health": d.health},
            "events": [{"event_id": e.event_id, "type": e.event_type, "observed_at": iso(e.observed_at),
                        "received_at": iso(e.received_at),
                        "latency_ms": round((aware(e.received_at) - aware(e.observed_at)).total_seconds() * 1000, 1),
                        "seq": e.sequence_number, "boot_id": e.boot_id, "payload": e.payload, "quality": e.quality,
                        "disposition": e.disposition, "source_mode": e.source_mode} for e in ev],
            "rejects": [{"received_at": iso(r.received_at), "code": r.reason_code, "reason": r.reason} for r in rj]}


@router.get("/telemetry/rejects", tags=["IoT"], summary="Журнал отклонённых сообщений")
def rejects(limit: int = Query(100, le=500), db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    return [{"received_at": iso(r.received_at), "topic": r.topic, "device_id": r.device_id, "code": r.reason_code,
             "reason": r.reason, "raw": r.raw[:400]}
            for r in db.execute(select(TelemetryReject).order_by(desc(TelemetryReject.id)).limit(limit)).scalars()]


@router.post("/observations/override", tags=["IoT"], summary="Ручное уточнение занятости пути (основание, срок, аудит)")
def override(body: S.OverrideIn, db: Session = Depends(get_db), user: User = Depends(require("observation.override")),
             idempotency_key: str | None = Header(default=None)):
    import uuid
    if not db.get(Track, body.object_id):
        raise NotFound("TRACK_NOT_FOUND", "Путь не найден.")

    def act():
        ov = ManualOverride(id=f"OV-{uuid.uuid4().hex[:8]}", object_id=body.object_id, attribute="occupancy",
                            value={"occupied": body.occupied}, reason=body.reason,
                            valid_until=utcnow() + timedelta(minutes=body.valid_minutes), user_id=user.id,
                            created_at=utcnow(), active=True)
        db.add(ov)
        sim = db.get(SimState, 1)
        audit(db, user, "observation.override", "track", body.object_id,
              f"Ручное уточнение: путь «{'занят' if body.occupied else 'свободен'}» на {body.valid_minutes} мин",
              reason=body.reason, after={"occupied": body.occupied, "valid_until": iso(ov.valid_until)},
              model_time=aware(sim.model_time))
        bump(db, "override")
        return {"id": ov.id, "valid_until": iso(ov.valid_until)}
    return run_idempotent(db, user, idempotency_key, "POST /observations/override", body.model_dump(), act)


@router.post("/observations/override/{ov_id}/cancel", tags=["IoT"], summary="Отменить ручное уточнение")
def cancel_override(ov_id: str, db: Session = Depends(get_db), user: User = Depends(require("observation.override"))):
    ov = db.get(ManualOverride, ov_id)
    if not ov:
        raise NotFound("OVERRIDE_NOT_FOUND", "Уточнение не найдено.")
    ov.active = False
    audit(db, user, "observation.override_cancel", "track", ov.object_id, "Ручное уточнение отменено")
    bump(db, "override_cancel")
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ индекс и настройки
@router.get("/index", tags=["Индекс"], summary="Текущий индекс эффективности с составляющими и качеством оценки")
def index_now(_: User = Depends(require("state.view"))):
    from app.services.hub import hub
    return hub.index_cache


@router.get("/index/config", tags=["Индекс"], summary="Активная конфигурация индекса и история версий")
def index_config(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    from app.services.index import COMPONENTS
    rows = list(db.execute(select(IndexConfig).order_by(desc(IndexConfig.version))).scalars())
    act = next((r for r in rows if r.active), rows[0])
    return {"active": {"version": act.version, "config": act.config, "created_at": iso(act.created_at), "created_by": act.created_by},
            "components": COMPONENTS,
            "history": [{"version": r.version, "created_at": iso(r.created_at), "created_by": r.created_by, "active": r.active}
                        for r in rows[:20]]}


@router.put("/index/config", tags=["Индекс"], summary="Изменить веса, нормализацию и пороги (новая версия, аудит)")
def put_index_config(body: S.IndexConfigIn, db: Session = Depends(get_db), user: User = Depends(require("config.manage"))):
    from app.services.index import validate_config
    cfg = {"weights": body.weights, "params": body.params, "thresholds": body.thresholds, "max_data_age_s": body.max_data_age_s}
    errs = validate_config(cfg)
    if errs:
        raise AppError("INVALID_INDEX_CONFIG", "Конфигурация индекса некорректна: " + " ".join(errs), details={"errors": errs})
    prev = db.execute(select(IndexConfig).where(IndexConfig.active.is_(True))).scalars().first()
    v = (db.execute(select(func.max(IndexConfig.version))).scalar() or 0) + 1
    if prev:
        prev.active = False
    db.add(IndexConfig(version=v, config=cfg, created_at=utcnow(), created_by=user.username, active=True))
    audit(db, user, "config.index", "index_config", str(v), f"Новая версия конфигурации индекса № {v}", reason=body.reason,
          before=prev.config if prev else None, after=cfg)
    db.commit()
    from app.services.hub import hub
    hub.index_cache = None
    return {"version": v}


@router.put("/config/plan-policy", tags=["Настройки"], summary="Политика месячного плана: цель или жёсткая квота")
def plan_policy(body: S.PolicyIn, db: Session = Depends(get_db), user: User = Depends(require("config.manage"))):
    p = db.execute(select(Plan)).scalars().first()
    before = p.policy
    p.policy = body.policy
    rule = db.execute(select(CapacityRule).where(CapacityRule.code == "MONTHLY_PLAN")).scalars().first()
    if rule:
        rule.policy = "hard" if body.policy == "hard_quota" else "soft"
    audit(db, user, "config.plan_policy", "plan", p.id, f"Политика месячного плана: {before} → {body.policy}", reason=body.reason,
          before={"policy": before}, after={"policy": body.policy})
    bump(db, "config:plan_policy")
    db.commit()
    return {"policy": p.policy}


@router.put("/config/device-thresholds", tags=["Настройки"], summary="Порог устаревания для типа источника")
def device_threshold(body: S.ThresholdIn, db: Session = Depends(get_db), user: User = Depends(require("config.manage"))):
    n = 0
    for d in db.execute(select(Device).where(Device.kind == body.kind)).scalars():
        d.stale_after_s = body.stale_after_s
        n += 1
    if not n:
        raise NotFound("DEVICE_KIND_NOT_FOUND", "Нет устройств такого типа.")
    audit(db, user, "config.device_threshold", "device_kind", body.kind, f"Порог устаревания {body.kind}: {body.stale_after_s} с")
    db.commit()
    return {"updated": n}


@router.get("/config/thresholds", tags=["Настройки"], summary="Пороги устаревания по типам источников")
def thresholds(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    rows = db.execute(select(Device.kind, func.min(Device.stale_after_s), func.min(Device.period_s), func.count())
                      .group_by(Device.kind)).all()
    return [{"kind": k, "stale_after_s": s, "period_s": p, "devices": n} for k, s, p, n in rows]


# ------------------------------------------------------------------ симуляция
@router.get("/sim", tags=["Симуляция"], summary="Состояние симуляции и список сценариев")
def sim_state(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    from app.sim.scenarios import SCENARIOS
    s = db.get(SimState, 1)
    return {"model_time": iso(s.model_time), "speed": s.speed, "running": s.running, "seed": s.seed, "scenario": s.scenario,
            "station_config": s.station_config, "scenarios": SCENARIOS,
            "scheduled_events": s.scheduled_events, "configs": {"large": "Крупная станция (Алматы, демо)",
                                                                "small": "Небольшая станция (Шамалган, демо)"}}


def _sim_audit(db, user, action, summary):
    s = db.get(SimState, 1)
    audit(db, user, action, "simulation", "1", summary, model_time=aware(s.model_time))


@router.post("/sim/start", tags=["Симуляция"], summary="Запустить модельное время")
def sim_start(db: Session = Depends(get_db), user: User = Depends(require("sim.control"))):
    s = db.get(SimState, 1, with_for_update=True)
    s.running = True
    _sim_audit(db, user, "sim.start", "Симуляция запущена")
    db.commit()
    return {"running": True}


@router.post("/sim/pause", tags=["Симуляция"], summary="Пауза")
def sim_pause(db: Session = Depends(get_db), user: User = Depends(require("sim.control"))):
    s = db.get(SimState, 1, with_for_update=True)
    s.running = False
    _sim_audit(db, user, "sim.pause", "Симуляция на паузе")
    db.commit()
    return {"running": False}


@router.post("/sim/speed", tags=["Симуляция"], summary="Изменить скорость модельного времени")
def sim_speed(body: S.SpeedIn, db: Session = Depends(get_db), user: User = Depends(require("sim.control"))):
    s = db.get(SimState, 1, with_for_update=True)
    s.speed = body.speed
    _sim_audit(db, user, "sim.speed", f"Скорость симуляции ×{body.speed:g}")
    db.commit()
    return {"speed": s.speed}


@router.post("/sim/reset", tags=["Симуляция"], summary="Сброс: сценарий + seed (воспроизводимое начальное состояние)")
def sim_reset(body: S.ResetIn, db: Session = Depends(get_db), user: User = Depends(require("sim.control"))):
    from app.services.hub import hub
    from app.sim.scenarios import SCENARIOS
    from app.sim.seed import reset_world
    if body.scenario not in SCENARIOS:
        raise AppError("UNKNOWN_SCENARIO", "Неизвестный сценарий.", details={"scenarios": list(SCENARIOS)})
    from sqlalchemy.exc import OperationalError
    from app.core.runtime_lock import world_lock
    cfg = body.station_config or db.get(SimState, 1).station_config
    db.rollback()  # не держим блокировок до захвата world_lock
    with world_lock:
        for attempt in range(3):
            try:
                reset_world(db, cfg, body.scenario, body.seed)
                _sim_audit(db, user, "sim.reset", f"Сброс симуляции: сценарий «{SCENARIOS[body.scenario]['title']}», "
                                                  f"seed {body.seed}, конфигурация {cfg}")
                db.commit()
                break
            except OperationalError:
                db.rollback()
                if attempt == 2:
                    raise AppError("RESET_BUSY", "Сброс не выполнен: база занята фоновыми операциями.", status=503,
                                   hint="Повторите через несколько секунд.")
        hub.reset_state()
    from app.core import bus
    bus.publish("plan_state_changed", {"reason": "reset"})
    return {"scenario": body.scenario, "seed": body.seed, "station_config": cfg}


@router.get("/sim/world", tags=["Симуляция"], include_in_schema=False)
def sim_world(x_sim_token: str | None = Header(default=None), db: Session = Depends(get_db)):
    if x_sim_token != get_settings().sim_world_token:
        raise Unauthorized("SIM_TOKEN_INVALID", "Доступ к эталонному миру симуляции только для симулятора.")
    from app.services.world import world
    return world(db)


# ------------------------------------------------------------------ журнал, история, отчёты
@router.get("/audit", tags=["Журнал"], summary="Аудит значимых изменений")
def audit_log(limit: int = Query(200, le=1000), db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    return [{"id": a.id, "ts": iso(a.ts), "model_time": iso(a.model_time), "username": a.username,
             "role": a.role, "role_label": role_label(a.role) if a.role != "system" else "Система", "action": a.action, "entity_type": a.entity_type,
             "entity_id": a.entity_id, "summary": a.summary, "reason": a.reason, "before": a.before, "after": a.after}
            for a in db.execute(select(AuditEvent).order_by(desc(AuditEvent.id)).limit(limit)).scalars()]


@router.get("/events", tags=["Журнал"], summary="Журнал доменных событий")
def events(limit: int = Query(200, le=1000), db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    return [{"id": e.id, "ts": iso(e.ts), "model_time": iso(e.model_time), "type": e.type, "severity": e.severity,
             "message": e.message, "payload": e.payload}
            for e in db.execute(select(DomainEvent).order_by(desc(DomainEvent.id)).limit(limit)).scalars()]


@router.get("/replay/range", tags=["История"], summary="Доступный для перемотки интервал")
def replay_range(db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    a = db.execute(select(func.min(StateSnapshot.real_time))).scalar()
    b = db.execute(select(func.max(StateDelta.real_time))).scalar()
    return {"from": iso(a), "to": iso(b), "retention_minutes": get_settings().replay_retention_minutes}


@router.get("/replay/window", tags=["История"], summary="Снимок + дельты для воспроизведения (режим «История» ничего не меняет)")
def replay_window(start: datetime = Query(alias="from"), end: datetime = Query(alias="to"), db: Session = Depends(get_db),
                  _: User = Depends(require("state.view"))):
    start, end = aware(start), aware(end)
    if end - start > timedelta(minutes=20):
        raise AppError("REPLAY_WINDOW_TOO_LONG", "Интервал воспроизведения не более 20 минут.")
    snap = db.execute(select(StateSnapshot).where(StateSnapshot.real_time <= start).order_by(desc(StateSnapshot.version)).limit(1)).scalars().first()
    if snap is None:
        snap = db.execute(select(StateSnapshot).order_by(StateSnapshot.version).limit(1)).scalars().first()
    if snap is None:
        raise NotFound("NO_HISTORY", "История ещё не накоплена.")
    deltas = db.execute(select(StateDelta).where(StateDelta.version > snap.version, StateDelta.real_time <= end)
                        .order_by(StateDelta.version)).scalars()
    return {"snapshot": {"version": snap.version, "real_time": iso(snap.real_time), "model_time": iso(snap.model_time),
                         "state": snap.state},
            "deltas": [{"version": d.version, "real_time": iso(d.real_time), "model_time": iso(d.model_time), "delta": d.delta}
                       for d in deltas]}


@router.get("/index/history", tags=["Индекс"], summary="Динамика индекса")
def index_history(limit: int = Query(200, le=2000), db: Session = Depends(get_db), _: User = Depends(require("state.view"))):
    rows = list(db.execute(select(IndexSnapshot).order_by(desc(IndexSnapshot.id)).limit(limit)).scalars())[::-1]
    return [{"real_time": iso(r.real_time), "model_time": iso(r.model_time), "value": r.value, "category": r.category,
             "components": r.components, "quality": r.quality, "config_version": r.config_version} for r in rows]


@router.get("/reports/mini", tags=["Отчёты"], summary="Мини-отчёт за период (PDF или CSV)")
def mini_report(format: str = Query("pdf", pattern="^(pdf|csv)$"), minutes: int = Query(60, ge=5, le=72 * 60),
                db: Session = Depends(get_db), _: User = Depends(require("report.export"))):
    from app.services.reports import collect, to_csv, to_pdf
    d = collect(db, minutes)
    stamp = utcnow().strftime("%Y%m%d-%H%M")
    if format == "csv":
        return Response(to_csv(d), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="station-report-{stamp}.csv"'})
    return Response(to_pdf(d), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="station-report-{stamp}.pdf"'})


@router.post("/assistant/ask", tags=["Помощник"], summary="Вопрос помощнику (объяснение серверных расчётов)")
def ask(body: S.AskIn, db: Session = Depends(get_db), _: User = Depends(require("assistant.ask"))):
    from app.services.assistant import answer
    return answer(db, body.question, body.context)


@router.get("/metrics", tags=["Служебные"], summary="Метрики задержек по этапам (p50/p95/max)")
def get_metrics(_: User = Depends(require("state.view"))):
    from app.iot.runtime import ingest_service
    from app.services.hub import hub
    m = metrics.snapshot()
    m["ingest"] = ingest_service.status()
    m["ws_clients"] = len(hub.clients)
    m["view_version"] = hub.version
    m["replanner"] = hub.replanner.last_result
    return m


@router.post("/metrics/reset", tags=["Служебные"], summary="Сбросить накопленные метрики (начало замера)")
def reset_metrics(_: User = Depends(require("sim.control"))):
    metrics.reset()
    return {"ok": True}


@router.post("/metrics/client", tags=["Служебные"], summary="Метрики отображения от клиента")
def client_metrics(body: S.ClientMetricsIn, _: User = Depends(current_user)):
    for s in body.samples[:200]:
        for k in ("client_render_ms", "event_to_screen_ms"):
            if isinstance(s.get(k), (int, float)) and 0 <= s[k] < 60000:
                metrics.observe(k, s[k])
    return {"ok": True}


# ------------------------------------------------------------------ администрирование: роли и пользователи
def _admin_call(db, user, key, endpoint, body, fn):
    out = run_idempotent(db, user, key, endpoint, body, fn)
    invalidate()  # права пользовательских ролей применяются сразу после фиксации
    return out


@router.get("/admin/roles", tags=["Доступ"], summary="Роли и их права (только администратор)")
def admin_roles(db: Session = Depends(get_db), _: User = Depends(require("users.manage"))):
    from app.services import admin
    out = admin.list_roles(db)
    db.commit()
    return out


@router.post("/admin/roles", tags=["Доступ"], summary="Создать пользовательскую роль (только администратор)")
def admin_create_role(body: S.RoleCreateIn, db: Session = Depends(get_db), user: User = Depends(require("users.manage")),
                      idempotency_key: str | None = Header(default=None)):
    from app.services import admin
    return _admin_call(db, user, idempotency_key, "POST /admin/roles", body.model_dump(),
                       lambda: admin.create_role(db, user, body.model_dump()))


@router.put("/admin/roles/{role_id}", tags=["Доступ"], summary="Изменить пользовательскую роль (только администратор)")
def admin_update_role(role_id: str, body: S.RoleUpdateIn, db: Session = Depends(get_db),
                      user: User = Depends(require("users.manage")), idempotency_key: str | None = Header(default=None)):
    from app.services import admin
    return _admin_call(db, user, idempotency_key, f"PUT /admin/roles/{role_id}", body.model_dump(),
                       lambda: admin.update_role(db, user, role_id, body.model_dump()))


@router.delete("/admin/roles/{role_id}", tags=["Доступ"], summary="Удалить пользовательскую роль (только администратор)")
def admin_delete_role(role_id: str, db: Session = Depends(get_db), user: User = Depends(require("users.manage")),
                      idempotency_key: str | None = Header(default=None)):
    from app.services import admin
    return _admin_call(db, user, idempotency_key, f"DELETE /admin/roles/{role_id}", {},
                       lambda: admin.delete_role(db, user, role_id))


@router.get("/admin/users", tags=["Доступ"], summary="Пользователи (только администратор)")
def admin_users(db: Session = Depends(get_db), _: User = Depends(require("users.manage"))):
    from app.services import admin
    return admin.list_users(db)


@router.post("/admin/users", tags=["Доступ"], summary="Создать пользователя и назначить роль (только администратор)")
def admin_create_user(body: S.UserCreateIn, db: Session = Depends(get_db), user: User = Depends(require("users.manage")),
                      idempotency_key: str | None = Header(default=None)):
    from app.services import admin
    safe = {k: v for k, v in body.model_dump().items() if k != "password"}
    return _admin_call(db, user, idempotency_key, "POST /admin/users", safe,
                       lambda: admin.create_user(db, user, body.model_dump()))


@router.put("/admin/users/{user_id}", tags=["Доступ"], summary="Сменить роль, имя или заблокировать пользователя")
def admin_update_user(user_id: str, body: S.UserUpdateIn, db: Session = Depends(get_db),
                      user: User = Depends(require("users.manage")), idempotency_key: str | None = Header(default=None)):
    from app.services import admin
    return _admin_call(db, user, idempotency_key, f"PUT /admin/users/{user_id}", body.model_dump(),
                       lambda: admin.update_user(db, user, user_id, body.model_dump()))


@router.post("/admin/users/{user_id}/password", tags=["Доступ"], summary="Задать пароль пользователю")
def admin_set_password(user_id: str, body: S.PasswordIn, db: Session = Depends(get_db),
                       user: User = Depends(require("users.manage"))):
    from app.services import admin
    out = admin.set_password(db, user, user_id, body.password)
    db.commit()
    return out
