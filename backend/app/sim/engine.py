"""Движок симуляции: модельное время и исполнение плана.

Движок — источник «физического мира» демонстрационной модели. Операция начинается только
при выполнении предусловий (предыдущая завершена, время наступило, путь и маршрут физически
свободны, путь не закрыт и имеет достоверные данные, ресурсы свободны и исправны, сосед
принимает). Если предусловие не выполнено, поезд ждёт — анимация не может «проехать» сквозь
занятый объект, потому что сама позиция вычисляется здесь, а не во frontend.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import domain_event
from app.core.timeutil import aware, local_hm, utcnow
from app.db import SessionLocal
from app.domain.statuses import can_transition
from app.models import Incident, Operation, SimState, Station, Track, Train, TransferRequest, Wagon
from app.services.forecast import forecast
from app.services.model import MOVEMENT_KINDS, StationModel
from app.services.versioning import bump, station_lock

log = logging.getLogger("engine")


class Engine:
    def __init__(self):
        self.last_mono = time.monotonic()
        self.waiting: dict[str, str] = {}   # train_id -> причина ожидания
        self.tick_model_dt = timedelta(seconds=10)
        self._prev_waiting: frozenset = frozenset()
        self.plan_flag = False

    # ------------------------------------------------------------------ шаг
    def step(self, advance: bool) -> bool:
        """Возвращает True, если плановое состояние изменилось."""
        mono = time.monotonic()
        dt_real = min(5.0, mono - self.last_mono)
        self.last_mono = mono
        with SessionLocal() as db:
            main = db.execute(select(Station).where(Station.kind == "main")).scalar_one_or_none()
            if main is None:
                return False
            station_lock(db, main.id)
            sim = db.get(SimState, 1, with_for_update=True)
            changed = False
            self.plan_flag = False
            if advance and sim.running:
                d = timedelta(seconds=dt_real * sim.speed)
                sim.model_time = aware(sim.model_time) + d
                self.tick_model_dt = d
            now = aware(sim.model_time)
            plan_changed = self.fire_events(db, sim, now)
            plan_changed |= self.resolve_expired(db, now)
            db.flush()
            model = StationModel(db)
            fc = forecast(model)
            changed = self.advance_ops(db, model, fc, now)
            if changed:
                db.flush()
                model = StationModel(db)
                fc = forecast(model)
            self.store_forecast(model, fc)
            changed |= self.requests_progress(db, model, now)
            sig = frozenset(self.waiting.items())
            if sig != self._prev_waiting:
                plan_changed = True  # появилось или исчезло ожидание — отклонение от плана
                self._prev_waiting = sig
            plan_changed |= self.plan_flag
            if plan_changed:
                bump(db, "engine")  # плановое состояние изменилось: рекомендации требуют перерасчёта
            db.commit()
            return changed or plan_changed

    # ------------------------------------------------------------------ события сценария
    def fire_events(self, db: Session, sim: SimState, now: datetime) -> bool:
        events = list(sim.scheduled_events or [])
        fired = False
        for ev in events:
            if ev.get("fired") or datetime.fromisoformat(ev["at"]) > now:
                continue
            try:
                ok = self._fire(db, sim, ev, now)
            except Exception as e:  # событие сценария не должно останавливать движок
                log.exception("Ошибка события сценария %s", ev)
                domain_event(db, "scenario.error", f"Событие сценария не выполнено: {e}", severity="warning",
                             model_time=now)
                ok = True
            if ok:
                ev["fired"] = True
                fired = True
            else:
                ev["at"] = (now + timedelta(minutes=5)).isoformat()
        sim.scheduled_events = [dict(e) for e in events]
        return fired

    def _fire(self, db: Session, sim: SimState, ev: dict, now: datetime) -> bool:
        from app.services.incidents import create_incident
        t = ev["type"]
        if t == "world":
            w = dict(sim.world or {})
            w[ev["key"]] = ev["value"]
            sim.world = w
            domain_event(db, "scenario.world", f"Сценарий: изменено состояние симулятора ({ev['key']})", model_time=now)
            return True
        if t == "device_fault":
            w = dict(sim.world or {})
            faults = dict(w.get("device_faults") or {})
            faults[ev["device_id"]] = {"mode": ev["mode"], "until": (utcnow() + timedelta(seconds=ev["duration_s"])).isoformat(),
                                       "then": ev.get("then")}
            w["device_faults"] = faults
            sim.world = w
            domain_event(db, "scenario.device_fault",
                         f"Сценарий (симуляция): устройство {ev['device_id']} перестаёт передавать данные на {ev['duration_s']} с",
                         severity="info", model_time=now)
            return True
        if t == "incident":
            model = StationModel(db)
            target = ev.get("target")
            req_cond = ev.get("requires")
            if req_cond and req_cond.startswith("request_confirmed:"):
                num = req_cond.split(":", 1)[1]
                r = db.execute(select(TransferRequest).where(TransferRequest.number == num)).scalar_one_or_none()
                if not r or r.status != "confirmed":
                    return False
            obj = self._resolve_target(db, model, target, now)
            if obj is None:
                return True
            kind = ev["kind"]
            start = None
            if ev.get("start_local_h"):
                from app.sim.seed import local_day
                start = (local_day(model.cfg) + timedelta(hours=ev["start_local_h"])).astimezone(now.tzinfo)
            create_incident(db, None, kind, obj, duration_min=ev.get("duration_min"), extra_min=ev.get("extra_min"),
                            title=ev.get("title") or (ev.get("title_tpl") and f"{ev['title_tpl']}: {model.track_label(obj)}"),
                            start_at=start, description="Событие демонстрационного сценария")
            return True
        return True

    def _resolve_target(self, db, model: StationModel, target: str | None, now):
        if target is None:
            return None
        if target == "busiest_rd_track":
            counts = {}
            for o in model.ops.values():
                if o.kind == "arrival" and o.status in ("planned", "confirmed") and \
                        model.track_rows[o.track_id].kind == "receiving_departure" and aware(o.planned_start) < now + timedelta(hours=3):
                    counts[o.track_id] = counts.get(o.track_id, 0) + 1
            return max(sorted(counts), key=lambda k: counts[k]) if counts else None
        if target.startswith("request_track:"):
            num = target.split(":", 1)[1]
            r = db.execute(select(TransferRequest).where(TransferRequest.number == num)).scalar_one_or_none()
            if r and r.train_id:
                arr = next((o for o in model.ops_by_train.get(r.train_id, []) if o.kind == "arrival"), None)
                return arr.track_id if arr else None
            return None
        if target == "active_cargo":
            ops = sorted((o for o in model.ops.values() if o.kind in ("unloading", "loading") and o.status != "done"),
                         key=lambda o: (o.status != "in_progress", o.planned_start))
            return ops[0].track_id if ops else None
        if target.startswith("rd_track:") or target.startswith("sorting_track:"):
            kind = "receiving_departure" if target.startswith("rd") else "sorting"
            i = int(target.split(":")[1])
            lst = sorted((t for t in model.track_rows.values() if t.kind == kind), key=lambda t: int(t.number))
            return lst[i].id if i < len(lst) else None
        if target.startswith("res:"):
            rid = target[4:]
            return rid if rid in model.resources else None
        if target == "east_neighbor":
            return next((n.id for n in model.neighbors.values() if n.config.get("side") == "east"), None)
        if target == "onstation_wagon":
            for t in sorted(model.trains.values(), key=lambda t: t.number):
                if t.status == "on_station" and any(o.kind == "departure" and o.status != "done"
                                                    for o in model.ops_by_train.get(t.id, [])):
                    wl = model.wagons_by_train.get(t.id)
                    if wl:
                        return sorted(wl, key=lambda w: w.position)[len(wl) // 2].id
            return None
        return target

    def resolve_expired(self, db: Session, now: datetime) -> bool:
        changed = False
        for inc in db.execute(select(Incident).where(Incident.status == "active", Incident.end_at.is_not(None))).scalars():
            if aware(inc.end_at) <= now:
                inc.status = "resolved"
                domain_event(db, "incident.resolved", f"Истёк срок инцидента: {inc.title}. Работа восстановлена.",
                             payload={"incident_id": inc.id}, model_time=now)
                changed = True
        return changed

    # ------------------------------------------------------------------ исполнение операций
    def advance_ops(self, db: Session, model: StationModel, fc: dict, now: datetime) -> bool:
        changed = False
        busy_switch = set()
        busy_res = set()
        moving_into: dict[str, str] = {}
        for o in model.ops.values():
            if o.status == "in_progress":
                if o.kind in MOVEMENT_KINDS:
                    busy_switch |= set(o.route_nodes or [])
                    if o.kind in ("arrival", "shunting"):
                        moving_into[o.track_id] = o.train_id
                busy_res |= set(o.resource_ids or [])
        standing = {t.current_track_id: t.id for t in model.trains.values() if t.status == "on_station" and t.current_track_id}
        closed = {i.object_id for i in model.incidents if i.kind == "track_closure" and aware(i.start_at) <= now}
        faulty_res = {i.object_id for i in model.incidents if i.kind == "resource_failure" and aware(i.start_at) <= now}
        restricted = {i.object_id for i in model.incidents if i.kind == "neighbor_restriction" and aware(i.start_at) <= now}
        unknown = {tid for tid, ds in model.data_states.items() if ds["state"] not in ("actual", "not_monitored")}
        self.waiting = {}

        def basis(o: Operation, prev_end):
            c = [aware(o.planned_start)]
            if o.not_before:
                c.append(aware(o.not_before))
            if prev_end:
                c.append(prev_end)
            t = model.trains.get(o.train_id) if o.train_id else None
            if o.kind == "arrival" and t and t.expected_arrival:
                c.append(aware(t.expected_arrival))
            return max(c)

        def can_start(o: Operation, train: Train | None) -> str | None:
            if not o.reserved:
                return "операция ещё не спланирована (нет резерва)"
            if o.kind in MOVEMENT_KINDS:
                common = set(o.route_nodes or []) & busy_switch
                if common:
                    return "маршрут занят другим движением"
                dest = o.track_id
                if o.kind in ("arrival", "shunting", "uncoupling"):
                    other = standing.get(dest)
                    if other and (train is None or other != train.id):
                        return f"{model.track_label(dest)} физически занят"
                    if moving_into.get(dest) and moving_into[dest] != (train.id if train else None):
                        return f"на {model.track_label_lc(dest)} уже выполняется заезд"
                    if dest in closed:
                        return f"{model.track_label(dest)} закрыт"
                    if dest in unknown and o.kind != "uncoupling":
                        return f"нет достоверных данных о состоянии: {model.track_label_lc(dest)}"
                if o.kind == "departure" and train and train.destination_station_id in restricted:
                    return "соседняя станция не принимает (ограничение)"
            for rid in o.resource_ids or []:
                if rid in busy_res:
                    return f"ресурс «{model.resources[rid].name}» занят"
                if rid in faulty_res:
                    return f"ресурс «{model.resources[rid].name}» неисправен"
            return None

        def start(o: Operation, train, b):
            nonlocal changed
            o.status = "in_progress"
            o.actual_start = max(b, now - self.tick_model_dt) if b < now else b
            if o.kind in MOVEMENT_KINDS:
                busy_switch.update(o.route_nodes or [])
                if o.kind in ("arrival", "shunting"):
                    moving_into[o.track_id] = o.train_id
            busy_res.update(o.resource_ids or [])
            changed = True

        def complete(o: Operation, train: Train | None, end: datetime):
            nonlocal changed
            o.status = "done"
            o.actual_end = min(end, now)
            changed = True
            busy_res.difference_update(o.resource_ids or [])
            if o.kind in MOVEMENT_KINDS:
                busy_switch.difference_update(o.route_nodes or [])
                moving_into.pop(o.track_id, None)
            if train is None:
                if o.kind == "repair":
                    m = re.search(r"№ (\d+)", o.note or "")
                    if m:
                        w = db.execute(select(Wagon).where(Wagon.number == m.group(1))).scalar_one_or_none()
                        if w:
                            w.condition = "ok"
                            domain_event(db, "wagon.repaired", f"Вагон № {w.number} отремонтирован", model_time=now)
                return
            if o.kind == "arrival":
                train.status = "on_station"
                train.current_track_id = o.track_id
                standing[o.track_id] = train.id
            elif o.kind == "shunting":
                standing.pop(train.current_track_id, None)
                train.current_track_id = o.track_id
                standing[o.track_id] = train.id
            elif o.kind == "uncoupling":
                for w in model.wagons_by_train.get(train.id, []):
                    if w.condition == "faulty":
                        w.train_id = None
                        w.condition = "in_repair"
                        train.wagons_count = max(0, train.wagons_count - 1)
                        domain_event(db, "wagon.uncoupled", f"Неисправный вагон № {w.number} отцеплен и подан в депо",
                                     model_time=now)
                        self.plan_flag = True
            elif o.kind == "departure":
                standing.pop(train.current_track_id, None)
                train.status = "departed"
                train.current_track_id = None
                domain_event(db, "train.departed", f"Поезд № {train.number} отправлен в {local_hm(o.actual_end)}",
                             model_time=now)
            elif o.kind == "sorting":
                standing.pop(train.current_track_id, None)
                train.status = "completed"
                train.current_track_id = None

        for tid, ops in model.ops_by_train.items():
            train = model.trains[tid]
            if train.status in ("departed", "completed", "cancelled"):
                continue
            prev_end = None
            for o in ops:
                if o.status == "cancelled":
                    continue
                if o.status == "done":
                    prev_end = aware(o.actual_end or o.planned_end)
                    continue
                if o.status == "in_progress":
                    end = aware(o.actual_start) + timedelta(minutes=o.duration_min + (o.extra_delay_min or 0))
                    if now >= end:
                        complete(o, train, end)
                        prev_end = min(end, now)
                        continue
                    break
                b = basis(o, prev_end)
                if now < b:
                    if o.kind == "arrival" and train.status == "scheduled" and b - now <= timedelta(minutes=40):
                        train.status = "approaching"
                        changed = True
                    break
                why = can_start(o, train)
                if why:
                    self.waiting[tid] = why
                    if o.kind == "arrival" and train.status in ("scheduled", "approaching") and can_transition("train", train.status, "waiting"):
                        train.status = "waiting"
                        changed = True
                    break
                start(o, train, b)
                end = aware(o.actual_start) + timedelta(minutes=o.duration_min + (o.extra_delay_min or 0))
                if now >= end:
                    complete(o, train, end)
                    prev_end = end
                    continue
                break
        for o in model.ops.values():
            if o.train_id or o.status in ("done", "cancelled"):
                continue
            if o.status == "in_progress":
                end = aware(o.actual_start) + timedelta(minutes=o.duration_min + (o.extra_delay_min or 0))
                if now >= end:
                    complete(o, None, end)
                continue
            b = basis(o, None)
            if now >= b and not can_start(o, None):
                start(o, None, b)
        return changed

    def store_forecast(self, model: StationModel, fc: dict):
        for oid, (s, e) in fc.items():
            o = model.ops.get(oid)
            if o is None:
                continue
            if o.forecast_start is None or abs((aware(o.forecast_start) - s).total_seconds()) >= 30:
                o.forecast_start = s
            if o.forecast_end is None or abs((aware(o.forecast_end) - e).total_seconds()) >= 30:
                o.forecast_end = e
        for tid, ops in model.ops_by_train.items():
            t = model.trains[tid]
            dep = next((o for o in ops if o.kind == "departure"), None)
            if dep and dep.id in fc and dep.status != "done":
                if t.expected_departure is None or abs((aware(t.expected_departure) - fc[dep.id][0]).total_seconds()) >= 30:
                    t.expected_departure = fc[dep.id][0]

    def requests_progress(self, db: Session, model: StationModel, now: datetime) -> bool:
        from app.services.requests import expire_overdue
        changed = False
        for r in db.execute(select(TransferRequest).where(TransferRequest.status.in_(["confirmed", "in_transit", "arrived"]))).scalars():
            t = model.trains.get(r.train_id) if r.train_id else None
            if r.status == "confirmed" and aware(r.desired_departure) <= now:
                r.status = "in_transit"
                domain_event(db, "request.in_transit", f"Заявка {r.number}: состав отправлен со станции отправления",
                             model_time=now)
                changed = True
            if r.status == "in_transit" and t and t.status in ("on_station", "completed"):
                r.status = "arrived"
                changed = True
            if r.status == "arrived" and t and t.status == "completed":
                r.status = "completed"
                changed = True
        if expire_overdue(db, now):
            changed = True
        return changed
