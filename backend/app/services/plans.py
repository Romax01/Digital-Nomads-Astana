"""Применение планов и ручные изменения операций.

Любое изменение плана (предложение планировщика, рекомендация, ручной перенос на Ганте,
перестановка очереди) проходит одинаковую серверную проверку всех ограничений на свежем
состоянии под блокировкой станции, затем атомарно заменяет резервы.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.audit import audit, domain_event
from app.core.errors import AppError, Conflict, NotFound
from app.core.timeutil import aware, iso, local_hm, utcnow
from app.domain.statuses import transition
from app.models import Operation, PlanVersion, Recommendation, Station, Train, User
from app.services.model import KIND_LABEL, MOVEMENT_KINDS, IntervalBook, StationModel, reservation_specs, with_presence
from app.services.reservations import create_from_ops, release_for_ops, release_for_train
from app.services.versioning import bump, station_lock


def _main(db):
    return db.execute(select(Station).where(Station.kind == "main")).scalar_one()


def validate_train(model: StationModel, train: Train | None, ops: list[Operation], new: dict[str, dict]) -> list[str]:
    """Проверка новой расстановки операций одного поезда относительно всех остальных резервов."""
    errors = []
    others = IntervalBook()
    for r in model.reservations:
        if train is not None and r.train_id == train.id:
            continue
        if train is None and r.operation_id in new:
            continue
        others.add(r.resource_key, aware(r.start_at), aware(r.end_at), type="reservation", label=r.purpose,
                   train_id=r.train_id)
    model.add_blocks(others, include_data_blocks=True)
    dicts = []
    prev_end = None
    for o in ops:
        if o.status in ("done", "cancelled"):
            continue
        e = new.get(o.id) or {"start": aware(o.planned_start), "end": aware(o.planned_end), "track_id": o.track_id,
                              "from_track_id": o.from_track_id, "resource_ids": o.resource_ids or [],
                              "route_nodes": o.route_nodes or []}
        if prev_end and e["start"] < prev_end and o.status != "in_progress":
            errors.append(f"{KIND_LABEL.get(o.kind)}: начало {local_hm(e['start'])} раньше окончания предыдущей операции "
                          f"({local_hm(prev_end)}) — нарушена технологическая последовательность")
        if o.not_before and e["start"] < aware(o.not_before) and o.status != "in_progress":
            errors.append(f"{KIND_LABEL.get(o.kind)}: нельзя начать раньше {local_hm(o.not_before)}")
        if e["start"] < model.now - timedelta(minutes=1) and o.status not in ("in_progress",):
            errors.append(f"{KIND_LABEL.get(o.kind)}: время {local_hm(e['start'])} уже прошло")
        prev_end = e["end"]
        dicts.append({"id": o.id, "kind": o.kind, "track_id": e["track_id"], "from_track_id": e.get("from_track_id"),
                      "start": e["start"], "end": e["end"], "resource_ids": e["resource_ids"],
                      "route_nodes": e["route_nodes"], "side": o.side, "status": o.status})
        tr = model.track_rows.get(e["track_id"])
        if tr and train is not None and train.kind not in (tr.allowed_train_kinds or []):
            errors.append(f"{model.track_label(tr.id)} не предназначен для поездов этого вида")
        L = model.train_length(train) if train else None
        if tr and L and tr.useful_length_m and tr.useful_length_m < L + model.cfg["processing"].get("length_margin_m", 10):
            errors.append(f"{model.track_label(tr.id)}: полезная длина {tr.useful_length_m:.0f} м меньше длины состава "
                          f"{L:.0f} м + запас")
        for rid in e["resource_ids"] or []:
            if not model.in_shift(rid, e["start"], e["end"]):
                errors.append(f"{model.resources[rid].name}: операция вне смены")
    dicts = with_presence(model, train, dicts)
    for sp in reservation_specs(model, train.id if train else None, train.number if train else "", dicts):
        hits = others.conflicts(sp["key"], sp["start"], sp["end"])
        for h in hits:
            if h.meta.get("type") == "shift":
                continue
            if h.meta.get("type") == "data" and sp["operation_id"] and str(sp["operation_id"]).startswith("presence-"):
                continue
            errors.append(f"{sp['purpose']} ({local_hm(sp['start'])}–{local_hm(sp['end'])}) пересекается: "
                          f"{h.meta.get('label') or h.meta.get('type')}")
            break
    return list(dict.fromkeys(errors))


def _apply_entries(db: Session, model: StationModel, ops_by_id: dict, entries: dict):
    for oid, e in entries.items():
        o = ops_by_id[oid]
        o.planned_start, o.planned_end = e["start"], e["end"]
        o.track_id = e["track_id"]
        o.from_track_id = e.get("from_track_id", o.from_track_id)
        o.resource_ids = list(e.get("resource_ids") or [])
        o.route_nodes = list(e.get("route_nodes") or [])
        o.reserved = True
        o.plan_version = (o.plan_version or 1) + 1


def apply_plan(db: Session, user: User, plan_id: int) -> dict:
    main = _main(db)
    station_lock(db, main.id)
    pv = db.get(PlanVersion, plan_id, with_for_update=True)
    if not pv:
        raise NotFound("PLAN_NOT_FOUND", "Предложенный план не найден.")
    model = StationModel(db)
    if pv.status != "proposed":
        raise Conflict("PLAN_NOT_PROPOSED", f"План уже в статусе «{pv.status}».")
    if pv.base_state_version != model.version:
        raise Conflict("PLAN_STALE",
                       f"Состояние станции изменилось после расчёта плана (версия {pv.base_state_version} → {model.version}). "
                       f"Применение устаревшего плана запрещено.", hint="Нажмите «Пересчитать план».",
                       details={"plan_version": pv.base_state_version, "current_version": model.version})
    schedule = {}
    for a in pv.assignments:
        o = model.ops.get(a["operation_id"])
        if o is None or o.status in ("done", "cancelled"):
            continue
        if o.status == "in_progress":  # началась после расчёта — фиксируется фактическое исполнение
            schedule[o.id] = {"start": aware(o.actual_start), "end": aware(o.actual_start) + timedelta(
                minutes=o.duration_min + (o.extra_delay_min or 0)), "track_id": o.track_id,
                "from_track_id": o.from_track_id, "resource_ids": o.resource_ids or [],
                "route_nodes": o.route_nodes or [], "fixed": True}
            continue
        schedule[o.id] = {"start": datetime.fromisoformat(a["start"]), "end": datetime.fromisoformat(a["end"]),
                          "track_id": a["track_id"], "from_track_id": a.get("from_track_id"),
                          "resource_ids": a.get("resource_ids") or [], "route_nodes": a.get("route_nodes") or [],
                          "fixed": a.get("fixed", False), "kept": a.get("kept", False)}
    from app.services.planner import verify
    errs = verify(model, {k: v for k, v in schedule.items() if k in model.ops})
    if errs:
        raise Conflict("PLAN_INVALID", "Повторная проверка плана выявила нарушения: " + "; ".join(errs[:3]),
                       details={"errors": errs}, hint="Пересчитайте план.")
    changed = {c["operation_id"] for c in pv.changes}
    entries = {oid: e for oid, e in schedule.items() if oid in changed and not e.get("fixed") and oid in model.ops}
    _apply_entries(db, model, model.ops, entries)
    trains = {model.ops[oid].train_id for oid in entries if model.ops[oid].train_id}
    db.flush()
    # сначала освобождаем резервы всех затронутых поездов, затем создаём новые — иначе новый
    # резерв одного поезда мог бы столкнуться со старым резервом другого, тоже переносимого
    standalone = [oid for oid in entries if not model.ops[oid].train_id]
    for tid in trains:
        release_for_train(db, tid)
    release_for_ops(db, standalone)
    db.flush()
    for tid in trains:
        t = model.trains[tid]
        ops = sorted(model.ops_by_train[tid], key=lambda o: o.seq)
        create_from_ops(db, model, tid, t.number, ops)
        dep = next((o for o in ops if o.kind == "departure"), None)
        if dep:
            t.expected_departure = dep.planned_start
    for oid in standalone:
        create_from_ops(db, model, None, model.ops[oid].note or "", [model.ops[oid]])
    transition("plan", pv, "applied")
    pv.applied_by = user.id
    db.execute(update(PlanVersion).where(PlanVersion.status == "proposed", PlanVersion.id != pv.id).values(status="stale"))
    db.execute(update(Recommendation).where(Recommendation.status == "active").values(status="stale"))
    for rec in db.execute(select(Recommendation).where(Recommendation.kind == "apply_plan")).scalars():
        if (rec.action or {}).get("plan_id") == pv.id:
            rec.status = "applied"
    audit(db, user, "plan.apply", "plan_version", str(pv.id),
          f"Применён план № {pv.id} ({pv.solver}, {pv.solver_status}): изменено операций — {len(entries)}",
          after={"changes": pv.changes, "summary": pv.summary}, model_time=model.now)
    domain_event(db, "plan.applied", f"Применён план № {pv.id}: изменено {len(entries)} операций, затронуто поездов: {len(trains)}",
                 payload={"plan_id": pv.id}, model_time=model.now)
    bump(db, "plan:apply")
    return {"plan_id": pv.id, "changed_operations": len(entries), "trains": sorted(model.trains[t].number for t in trains)}


def apply_train_moves(db: Session, user: User, train_id: str, moves: list[dict], reason: str) -> dict:
    main = _main(db)
    station_lock(db, main.id)
    model = StationModel(db)
    train = model.trains.get(train_id)
    if not train:
        raise NotFound("TRAIN_NOT_FOUND", "Поезд не найден.")
    ops = sorted(model.ops_by_train.get(train_id, []), key=lambda o: o.seq)
    by_id = {o.id: o for o in ops}
    entries = {}
    for mv in moves:
        o = by_id.get(mv["operation_id"])
        if not o:
            raise AppError("OPERATION_NOT_IN_TRAIN", "Операция не относится к поезду.")
        if o.status in ("in_progress", "done"):
            raise Conflict("OPERATION_STARTED", f"{KIND_LABEL.get(o.kind)} уже начата — перенос невозможен.")
        s = datetime.fromisoformat(mv["start"].replace("Z", "+00:00"))
        entries[o.id] = {"start": s, "end": s + timedelta(minutes=o.duration_min + (o.extra_delay_min or 0)),
                         "track_id": mv["track_id"], "from_track_id": mv.get("from_track_id"),
                         "resource_ids": mv.get("resource_ids", o.resource_ids), "route_nodes": mv.get("route_nodes", o.route_nodes)}
    errs = validate_train(model, train, ops, entries)
    if errs:
        raise Conflict("CONSTRAINTS_VIOLATED", "Изменение не прошло проверку ограничений: " + "; ".join(errs[:3]),
                       details={"errors": errs}, hint="Выберите другое время или путь; проверка выполняется на сервере.")
    before = [{"id": o.id, "start": iso(o.planned_start), "track": o.track_id} for o in ops if o.id in entries]
    _apply_entries(db, model, by_id, entries)
    db.flush()
    release_for_train(db, train.id)
    db.flush()
    create_from_ops(db, model, train.id, train.number, ops)
    dep = next((o for o in ops if o.kind == "departure"), None)
    if dep:
        train.expected_departure = dep.planned_start
    audit(db, user, "operation.reschedule", "train", train.id, f"Изменены операции поезда № {train.number}: {reason}",
          before={"ops": before}, after={"ops": [{"id": k, "start": iso(v["start"]), "track": v["track_id"]} for k, v in entries.items()]},
          reason=reason, model_time=model.now)
    domain_event(db, "plan.changed", f"Изменены операции поезда № {train.number}: {reason}", payload={"train_id": train.id},
                 model_time=model.now)
    bump(db, "operation:reschedule")
    return {"train_id": train.id, "changed_operations": len(entries)}


def reschedule_operation(db: Session, user: User, op_id: str, start: datetime, track_id: str | None, reason: str) -> dict:
    """Ручной перенос операции (например, с диаграммы Ганта). Последующие операции цепочки
    сдвигаются не раньше окончания предыдущих; маршруты пересчитываются по топологии."""
    model = StationModel(db)
    o = model.ops.get(op_id)
    if not o:
        raise NotFound("OPERATION_NOT_FOUND", "Операция не найдена.")
    if o.status in ("in_progress", "done", "cancelled"):
        raise Conflict("OPERATION_STARTED", "Начатую или завершённую операцию перенести нельзя.")
    start = aware(start)
    if not o.train_id:
        moves = [{"operation_id": o.id, "start": iso(start), "track_id": track_id or o.track_id}]
        errs = validate_train(model, None, [o], {o.id: {"start": start, "end": start + timedelta(minutes=o.duration_min),
                                                        "track_id": track_id or o.track_id, "resource_ids": o.resource_ids,
                                                        "route_nodes": o.route_nodes}})
        if errs:
            raise Conflict("CONSTRAINTS_VIOLATED", "Изменение не прошло проверку ограничений: " + "; ".join(errs[:3]),
                           details={"errors": errs})
        main = _main(db)
        station_lock(db, main.id)
        o.planned_start, o.planned_end = start, start + timedelta(minutes=o.duration_min)
        o.track_id, o.reserved = track_id or o.track_id, True
        release_for_ops(db, [o.id])
        db.flush()
        create_from_ops(db, model, None, o.note or "", [o])
        audit(db, user, "operation.reschedule", "operation", o.id, f"Перенос: {o.note}", reason=reason, model_time=model.now)
        bump(db, "operation:reschedule")
        return {"changed_operations": 1}
    ops = sorted(model.ops_by_train[o.train_id], key=lambda x: x.seq)
    moves = []
    t = None
    topo = model.topo
    stay_track = track_id
    started = False
    for x in ops:
        if x.id == o.id:
            started = True
            t = start
        if not started or x.status in ("done", "in_progress", "cancelled"):
            continue
        if x.id != o.id:
            t = max(aware(x.planned_start), t)
        from_track = x.from_track_id
        if stay_track and x.id != o.id and x.kind in ("arrival", "shunting"):
            from_track = stay_track if x.kind == "shunting" else from_track  # выезд с нового пути
            stay_track = None  # смена пути действует только на текущую стоянку
        new_track = (stay_track or x.track_id) if x.kind not in ("uncoupling",) else x.track_id
        if x.kind == "uncoupling" and stay_track:
            from_track = stay_track
        route_nodes = x.route_nodes or []
        if x.kind in MOVEMENT_KINDS:
            if x.kind == "arrival":
                r = topo.arrival_route(x.side or "west", new_track)
            elif x.kind == "departure":
                r = topo.departure_route(x.side or "east", new_track)
            else:
                r = topo.shunting_route(from_track, new_track) if from_track else None
            if r is None:
                raise Conflict("NO_ROUTE", f"Нет маршрута по топологии для операции «{KIND_LABEL.get(x.kind)}» на "
                                           f"{model.track_label_lc(new_track)}.")
            route_nodes = r.switch_ids
        moves.append({"operation_id": x.id, "start": iso(t), "track_id": new_track, "from_track_id": from_track,
                      "resource_ids": x.resource_ids, "route_nodes": route_nodes})
        t = t + timedelta(minutes=x.duration_min + (x.extra_delay_min or 0))
    return apply_train_moves(db, user, o.train_id, moves, reason)
