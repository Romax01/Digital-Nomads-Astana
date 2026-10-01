"""Оркестратор realtime.

Цикл (≥1 Гц): шаг движка → сборка состояния → дельта с номером версии → рассылка клиентам
WebSocket → сохранение дельты (и периодического снимка) для перемотки. Внеочередная сборка
запускается событиями шины (телеметрия, команды), но не чаще чем раз в 100 мс.

Перепланирование выполняется в отдельном потоке: близкие события объединяются (debounce),
результат, рассчитанный для устаревшей версии состояния, отбрасывается.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from datetime import timedelta

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.core import bus, metrics
from app.core.audit import domain_event
from app.core.timeutil import aware, iso, utcnow
from app.db import SessionLocal
from app.models import (
    AuditEvent, IdempotencyKey, IndexSnapshot, PlanVersion, Recommendation, Station, StateDelta, StateSnapshot, TelemetryEvent,
    TelemetryReject,
)
from app.services.model import StationModel
from app.services.versioning import bump

log = logging.getLogger("hub")
RESOLVABLE = {"track_overlap", "track_closed", "maintenance", "data_unknown", "route_conflict", "resource_conflict",
              "resource_unavailable", "neighbor_restriction", "unplanned"}


class Client:
    def __init__(self, ws, user):
        self.ws = ws
        self.user = user
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.need_snapshot = False
        self.rtt_ms: float | None = None
        self.limited = False  # только события процесса работников (без снимка станции)


class Hub:
    def __init__(self):
        from app.sim.engine import Engine
        self.engine = Engine()
        self.state: dict | None = None
        self.version = 0
        self.ring: deque = deque(maxlen=600)
        self.clients: set[Client] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.dirty: asyncio.Event | None = None
        self.trace_received_at: float | None = None   # wall-clock (с) самого раннего необработанного события
        self.trace_mono: float | None = None
        self._lock = threading.Lock()
        self.index_cache = None
        self.index_mono = 0.0
        self.index_saved_mono = 0.0
        self.snapshot_mono = 0.0
        self.cleanup_mono = time.monotonic()
        self.prev_data_cats: dict | None = None
        self.reset_mono = time.monotonic()
        self.replanner = Replanner(self)
        self.running = False

    # ------------------------------------------------------------- шина
    def push_work_event(self, topic: str, payload: dict):
        """Событие процесса работников — только тем клиентам, кому объект виден по правилам REST.
        Проверка видимости читает БД, поэтому выполняется в фоновом потоке, а не в потоке запроса."""
        if not self.loop:
            return
        threading.Thread(target=self._push_work_event, args=(topic, payload), daemon=True,
                         name="work-events").start()

    def _push_work_event(self, topic: str, payload: dict):
        from app.models import DefectReport, WorkOrder
        from app.services import workflow as wf
        targets = []
        try:
            with SessionLocal() as db:
                obj = None
                if topic == "work_changed":
                    obj = (db.get(DefectReport, payload["entity_id"]) if payload["entity_type"] == "defect"
                           else db.get(WorkOrder, payload["entity_id"]))
                for c in list(self.clients):
                    u = db.get(type(c.user), c.user.id)
                    if not u or not u.active:
                        continue
                    if topic == "notification":
                        ok = payload.get("user_id") == u.id
                    elif obj is None:
                        ok = False
                    elif payload["entity_type"] == "defect":
                        ok = wf.can_view_defect(db, u, obj)
                    else:
                        ok = wf.can_view_wo(db, u, obj)
                    if ok:
                        targets.append(c)
        except Exception:
            log.exception("Ошибка рассылки события работников")
            return
        msg = json.dumps({"type": "work", "topic": topic, **({k: payload[k] for k in ("entity_type", "entity_id")}
                                                            if topic == "work_changed" else {}),
                          "server_time": time.time()}, ensure_ascii=False)

        def put():
            for c in targets:
                try:
                    c.queue.put_nowait(msg)
                except asyncio.QueueFull:
                    pass
        self.loop.call_soon_threadsafe(put)

    def on_bus(self, topic: str, payload: dict):
        if topic in ("work_changed", "notification"):
            self.push_work_event(topic, payload)
            return
        if topic == "telemetry_applied" and payload.get("received_wall"):
            with self._lock:
                if self.trace_received_at is None:
                    self.trace_received_at = payload["received_wall"]
                    self.trace_mono = payload.get("received_mono")
        if topic == "plan_state_changed":
            self.replanner.trigger(payload.get("reason", "change"))
        if self.loop and self.dirty:
            self.loop.call_soon_threadsafe(self.dirty.set)

    # ------------------------------------------------------------- цикл
    async def run(self):
        s = get_settings()
        self.loop = asyncio.get_running_loop()
        self.dirty = asyncio.Event()
        with SessionLocal() as db:
            self.version = db.execute(select(func.coalesce(func.max(StateDelta.version), 0))).scalar_one()
            self.version = max(self.version, db.execute(select(func.coalesce(func.max(StateSnapshot.version), 0))).scalar_one())
        self.running = True
        next_tick = self.loop.time()
        last_build = 0.0
        self.replanner.trigger("startup")
        while self.running:
            timeout = max(0.0, next_tick - self.loop.time())
            try:
                await asyncio.wait_for(self.dirty.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            self.dirty.clear()
            since = self.loop.time() - last_build
            if since < 0.1:
                await asyncio.sleep(0.1 - since)
            advance = self.loop.time() >= next_tick
            if advance:
                next_tick = max(next_tick + s.sim_tick_seconds, self.loop.time() + 0.2)
            try:
                msgs = await asyncio.to_thread(self.step, advance)
            except Exception:
                log.exception("Ошибка шага realtime")
                msgs = []
            last_build = self.loop.time()
            for m in msgs:
                await self.broadcast(m)

    def step(self, advance: bool) -> list[dict]:
        from app.core.runtime_lock import world_lock
        with world_lock:
            return self._step(advance)

    def _step(self, advance: bool) -> list[dict]:
        s = get_settings()
        if s.engine_enabled:
            self.engine.step(advance)
        t0 = time.perf_counter()
        with self._lock:
            trace_at, trace_mono = self.trace_received_at, self.trace_mono
            self.trace_received_at = self.trace_mono = None
        msgs = []
        with SessionLocal() as db:
            if db.execute(select(Station).where(Station.kind == "main")).first() is None:
                return []
            model = StationModel(db)
            self.monitor_data_quality(db, model)
            mono = time.monotonic()
            if self.index_cache is None or mono - self.index_mono > 5:
                from app.services.conflicts import detect
                from app.services.index import compute_current
                det = detect(model)
                idx = compute_current(db, model, det["conflicts"])
                if mono - self.index_saved_mono > 15 or self.index_cache is None:
                    db.add(IndexSnapshot(model_time=model.now, real_time=utcnow(), value=idx["value"],
                                         category=idx["category"], config_version=idx["config_version"],
                                         components={k: c["score"] for k, c in idx["components"].items()},
                                         quality=idx["quality"]))
                    self.index_saved_mono = mono
                trend = [{"t": iso(r.real_time), "m": iso(r.model_time), "v": r.value} for r in db.execute(
                    select(IndexSnapshot).order_by(IndexSnapshot.id.desc()).limit(40)).scalars()][::-1]
                idx["trend"] = trend
                self.index_cache, self.index_mono = idx, mono
            from app.services.view import build_view, diff
            view = build_view(db, model, self.engine.waiting, self.index_cache)
            build_ms = (time.perf_counter() - t0) * 1000
            metrics.observe("state_build_ms", build_ms)
            d = diff(self.state, view)
            if d.get("full") or d:
                self.version += 1
                v = self.version
                now_wall = time.time()
                trace = None
                if trace_at:
                    trace = {"event_received_at": trace_at, "built_at": now_wall}
                    metrics.observe("model_update_ms", (time.monotonic() - trace_mono) * 1000 if trace_mono else 0)
                if d.get("full"):
                    msg = {"type": "snapshot", "version": v, "state": view, "server_time": now_wall}
                else:
                    msg = {"type": "delta", "version": v, "base": v - 1, "delta": d, "server_time": now_wall,
                           "trace": trace}
                    self.ring.append((v, msg))
                    db.add(StateDelta(version=v, real_time=utcnow(), model_time=model.now, delta=d))
                self.state = view
                if d.get("full") or time.monotonic() - self.snapshot_mono > s.snapshot_every_s:
                    db.add(StateSnapshot(version=v, real_time=utcnow(), model_time=model.now, state=view))
                    self.snapshot_mono = time.monotonic()
                msgs.append(msg)
            db.commit()
        if time.monotonic() - self.cleanup_mono > 300:
            self.cleanup_mono = time.monotonic()
            self.cleanup()
        return msgs

    def monitor_data_quality(self, db, model: StationModel):
        """Переход пути в «Неизвестно» и обратно — изменение планового состояния (версия + событие)."""
        cats = {tid: ds["state"] for tid, ds in model.data_states.items()}
        prev = self.prev_data_cats
        self.prev_data_cats = cats
        if prev is None:
            return
        changed = False
        # первые секунды после сброса/старта датчики только начинают присылать данные — это не
        # «потеря» и не «восстановление»: версия состояния меняется, журнал не засоряется
        warmup = time.monotonic() - self.reset_mono < 20
        for tid, st in cats.items():
            p = prev.get(tid)
            if p == st:
                continue
            if warmup and (p in (None, "missing") or st == "missing"):
                changed = True
                continue
            bad = st not in ("actual", "not_monitored")
            was_bad = p not in (None, "actual", "not_monitored")
            if bad != was_bad or (bad and p != st):
                changed = True
                label = model.track_label(tid)
                if bad:
                    domain_event(db, "data.quality", model.data_states[tid]["message"], severity="warning",
                                 payload={"track_id": tid, "state": st}, model_time=model.now)
                else:
                    domain_event(db, "data.quality", f"Данные занятости ({label[:1].lower() + label[1:]}) восстановлены и подтверждены "
                                                     f"датчиком: «{'занят' if model.data_states[tid].get('observed') == 'occupied' else 'свободен'}».",
                                 payload={"track_id": tid, "state": st}, model_time=model.now)
        if changed:
            bump(db, "data_quality")
            db.commit()
            model.version += 1

    async def broadcast(self, msg: dict):
        if not self.clients:
            return
        text = json.dumps(msg, ensure_ascii=False, default=str)
        sent_at = time.time()
        if msg.get("trace") and msg["trace"].get("event_received_at"):
            metrics.observe("event_to_ws_ms", (sent_at - msg["trace"]["event_received_at"]) * 1000)
        for c in list(self.clients):
            if c.need_snapshot or c.limited:  # работникам состояние станции не рассылается
                continue
            try:
                c.queue.put_nowait(text)
            except asyncio.QueueFull:
                # клиент не успевает: очищаем очередь и отправим полный снимок
                c.need_snapshot = True
                while not c.queue.empty():
                    c.queue.get_nowait()
                c.queue.put_nowait("__SNAPSHOT__")

    def snapshot_message(self) -> str:
        return json.dumps({"type": "snapshot", "version": self.version, "state": self.state,
                           "server_time": time.time()}, ensure_ascii=False, default=str)

    def catch_up(self, last_version: int) -> list[str] | None:
        """Дельты после last_version из кольцевого буфера; None — нужен полный снимок."""
        if self.state is None:
            return None
        if last_version == self.version:
            return []
        items = [m for v, m in self.ring if v > last_version]
        if not items or items[0]["version"] != last_version + 1:
            return None
        return [json.dumps(m, ensure_ascii=False, default=str) for m in items]

    def cleanup(self):
        s = get_settings()
        now = utcnow()
        try:
            with SessionLocal() as db:
                db.execute(delete(TelemetryEvent).where(TelemetryEvent.received_at < now - timedelta(hours=s.telemetry_retention_hours)))
                db.execute(delete(TelemetryReject).where(TelemetryReject.received_at < now - timedelta(hours=s.telemetry_retention_hours)))
                cut = now - timedelta(minutes=s.replay_retention_minutes)
                db.execute(delete(StateDelta).where(StateDelta.real_time < cut))
                keep = db.execute(select(func.max(StateSnapshot.version)).where(StateSnapshot.real_time < cut)).scalar()
                if keep:
                    db.execute(delete(StateSnapshot).where(StateSnapshot.version < keep))
                db.execute(delete(IndexSnapshot).where(IndexSnapshot.real_time < now - timedelta(hours=72)))
                db.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < now - timedelta(hours=48)))
                from app.api.work import cleanup_attachments
                from app.services.workflow import check_overdue
                cleanup_attachments(db)
                check_overdue(db)
                db.execute(delete(AuditEvent).where(AuditEvent.ts < now - timedelta(days=s.audit_retention_days)))
                db.commit()
        except Exception:
            log.exception("Ошибка очистки истории")

    def reset_state(self):
        """После сброса сценария клиенты получают полный снимок."""
        self.state = None
        self.prev_data_cats = None
        self.reset_mono = time.monotonic()
        self.index_cache = None
        self.ring.clear()
        for c in list(self.clients):
            c.need_snapshot = True
            try:
                c.queue.put_nowait("__SNAPSHOT__")
            except asyncio.QueueFull:
                pass


class Replanner:
    """Фоновое перепланирование с объединением событий и защитой от устаревших результатов."""

    def __init__(self, hub: Hub):
        self.hub = hub
        self.event = threading.Event()
        self.last_trigger = 0.0
        self.reason = ""
        self.thread: threading.Thread | None = None
        self.enabled = True
        self.last_result: dict | None = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="replanner", daemon=True)
            self.thread.start()

    def trigger(self, reason: str):
        self.last_trigger = time.monotonic()
        self.reason = reason
        self.event.set()

    MIN_INTERVAL_S = 10.0  # не чаще раза в 10 с: построение модели CP-SAT занимает GIL процесса

    def _loop(self):
        s = get_settings()
        last_run = 0.0
        while True:
            self.event.wait()
            while time.monotonic() - self.last_trigger < s.replan_debounce_s or                     time.monotonic() - last_run < self.MIN_INTERVAL_S:
                time.sleep(0.2)
            self.event.clear()
            last_run = time.monotonic()
            if not self.enabled:
                continue
            try:
                self.run_once(self.reason)
            except Exception:
                log.exception("Ошибка автоматического перепланирования")

    def run_once(self, reason: str, force: bool = False) -> dict | None:
        return self._run_once(reason, force)

    def _run_once(self, reason: str, force: bool = False) -> dict | None:
        from app.core.runtime_lock import world_lock
        from app.services.conflicts import detect
        from app.services.planner import run_planner
        # Все обращения к БД — под world_lock и с закрытием транзакции до расчёта: иначе открытая
        # транзакция планировщика и TRUNCATE при сбросе сценария блокируют друг друга (PostgreSQL
        # такую межсессионную взаимоблокировку не распознаёт).
        with SessionLocal() as db, world_lock:
            if db.execute(select(Station).where(Station.kind == "main")).first() is None:
                return None
            model = StationModel(db)
            existing = db.execute(select(PlanVersion.id).where(
                PlanVersion.status == "proposed", PlanVersion.base_state_version == model.version)).first()
            db.commit()
        missing = sum(1 for ds in model.data_states.values() if ds["state"] == "missing")
        if not force and model.data_states and missing > len(model.data_states) / 2:
            log.info("Перепланирование отложено: нет данных датчиков по %d путям", missing)
            return None
        det = detect(model)
        relevant = [c for c in det["conflicts"] if c["type"] in RESOLVABLE]
        if not relevant and not force:
            return None
        if existing and not force:
            return None
        res = run_planner(model, trigger=reason)  # без блокировок и открытых транзакций
        with world_lock:
            pv_id = save_plan(model, res, reason, relevant)
        if pv_id is None:
            metrics.incr("replan_discarded_stale")
            self.trigger("retry_after_stale")
            return None
        self.last_result = {"plan_id": pv_id, "status": res["status"], "solve_ms": res["solve_ms"]}
        if self.hub.loop and self.hub.dirty:
            self.hub.loop.call_soon_threadsafe(self.hub.dirty.set)
        return {"plan_id": pv_id, **res}


def save_plan(model: StationModel, res: dict, trigger: str, conflicts: list[dict]) -> int | None:
    """Сохраняет предложение, если версия состояния не изменилась за время расчёта."""
    from app.services.versioning import current
    with SessionLocal() as db:
        if current(db) != model.version:
            return None
        db.query(PlanVersion).filter(PlanVersion.status == "proposed").update({"status": "stale"})
        db.query(Recommendation).filter(Recommendation.status == "active", Recommendation.kind == "apply_plan").update({"status": "stale"})
        assignments = [{"operation_id": oid, "start": e["start"].isoformat(), "end": e["end"].isoformat(),
                        "track_id": e["track_id"], "from_track_id": e.get("from_track_id"),
                        "resource_ids": e.get("resource_ids") or [], "route_nodes": e.get("route_nodes") or [],
                        "fixed": e.get("fixed", False), "kept": e.get("kept", False)} for oid, e in res["schedule"].items()]
        summary = {**res["summary"], "status_label": res["status_label"], "notes": res["notes"],
                   "unresolved": res["unresolved"], "total_ms": res["total_ms"], "objective": res["objective"]}
        pv = PlanVersion(station_id=model.sid, created_at=utcnow(), model_time=model.now, base_state_version=model.version,
                         trigger=trigger, status="proposed", solver=res["solver"], solver_status=res["status"],
                         solve_ms=res["solve_ms"], summary=summary, changes=res["changes"], assignments=assignments,
                         assumptions=res["assumptions"])
        db.add(pv)
        db.flush()
        if res["changes"] and res["status"] in ("optimal", "feasible", "heuristic", "partial"):
            sb, sa = res["summary"]["before"], res["summary"]["after"]
            ib, ia = res["summary"]["index_before"], res["summary"]["index_after"]
            trains = sorted({c["train"] for c in res["changes"] if c["train"]})
            reason = "; ".join(c["explanation"] for c in conflicts[:3]) or "Улучшение плана"
            db.add(Recommendation(
                id=f"REC-{pv.id}", station_id=model.sid, kind="apply_plan",
                title=f"Применить план № {pv.id}: изменить {len(res['changes'])} операций ({len(trains)} поезд.)",
                reason=reason,
                affected=[{"type": "train", "id": next((c["train_id"] for c in res["changes"] if c["train"] == t), None),
                           "label": f"Поезд № {t}"} for t in trains] +
                         [{"type": "track", "id": tid, "label": model.track_label(tid)}
                          for tid in sorted({x for c in res["changes"] if c["track_changed"] for x in c.get("track_ids", [])})] +
                         [{"type": "conflict", "id": c["id"], "label": c["title"]} for c in conflicts[:5]],
                action={"type": "apply_plan", "plan_id": pv.id},
                effect={"conflicts_before": res["summary"]["conflicts_before"], "conflicts_after": sa["conflicts"],
                        "delay_before_min": sb["total_delay_min"], "delay_after_min": sa["total_delay_min"],
                        "weighted_delay_before": sb["weighted_delay"], "weighted_delay_after": sa["weighted_delay"],
                        "index_before": ib.get("value"), "index_after": ia.get("value"),
                        "solver_status": res["status_label"], "solve_ms": res["solve_ms"]},
                computed_at=model.now, computed_real_at=utcnow(), based_on_version=model.version, status="active"))
        db.commit()
        return pv.id


hub = Hub()
bus.subscribe("*", hub.on_bus)
