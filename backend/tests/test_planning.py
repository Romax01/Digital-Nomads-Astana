"""Перепланирование, движок симуляции, 5/10 одновременных инцидентов, ручной перенос,
воспроизведение истории, восстановление WebSocket, индекс, помощник, отчёты."""
import json
import time
from datetime import timedelta

from sqlalchemy import select, text

from app.core.timeutil import aware
from app.db import SessionLocal
from app.models import Incident, Operation, SimState, StateDelta, StateSnapshot, TransferRequest
from app.services.conflicts import detect
from app.services.model import StationModel
from app.services.planner import run_planner, verify
from tests.conftest import fresh_observations, login

OVERLAP_SQL = """SELECT count(*) FROM reservations a JOIN reservations b ON a.resource_key=b.resource_key
    AND a.id < b.id AND a.status='confirmed' AND b.status='confirmed'
    AND tstzrange(a.start_at,a.end_at) && tstzrange(b.start_at,b.end_at)"""


def _advance(minutes: float, steps: int = 1):
    from app.sim.engine import Engine
    eng = Engine()
    for _ in range(steps):
        with SessionLocal() as db:
            s = db.get(SimState, 1)
            s.model_time = aware(s.model_time) + timedelta(minutes=minutes / steps)
            db.commit()
            fresh_observations(db)
        eng.step(advance=False)
    return eng


def _apply_plan_from(res, model, user="u-station"):
    from app.models import User
    from app.services.hub import save_plan
    from app.services.plans import apply_plan
    pid = save_plan(model, res, "test", [])
    assert pid is not None
    with SessionLocal() as db:
        out = apply_plan(db, db.get(User, user), pid)
        db.commit()
    return out


def test_track_closure_conflict_and_replan(world):
    world("normal")
    from app.services.incidents import create_incident
    from app.sim.engine import Engine
    with SessionLocal() as db:
        model = StationModel(db)
        tid = Engine()._resolve_target(db, model, "busiest_rd_track", model.now)
        create_incident(db, None, "track_closure", tid, duration_min=150)
        db.commit()
        fresh_observations(db)
        model = StationModel(db)
        before = [c for c in detect(model)["conflicts"] if c["type"] == "track_closed" and c["severity"] == "critical"]
        assert before, "закрытие пути с запланированными операциями даёт конфликт"
        res = run_planner(model)
        assert res["status"] in ("optimal", "feasible", "heuristic"), res["notes"]
        assert verify(model, {k: v for k, v in res["schedule"].items() if not v.get("kept")}) == []
        assert res["changes"]
        assert res["summary"]["index_after"]["value"] is not None
    with SessionLocal() as db:
        _apply_plan_from(res, StationModel(db))
    with SessionLocal() as db:
        fresh_observations(db)
        after = [c for c in detect(StationModel(db))["conflicts"] if c["type"] == "track_closed" and c["severity"] == "critical"]
        assert after == []
        assert db.execute(text(OVERLAP_SQL)).scalar() == 0


def _multi(world, scenario, n):
    world(scenario)
    _advance(3)
    with SessionLocal() as db:
        incs = db.execute(select(Incident).where(Incident.status == "active")).scalars().all()
        assert len(incs) >= n - 1, [i.title for i in incs]
        fresh_observations(db)
        model = StationModel(db)
        t0 = time.perf_counter()
        res = run_planner(model)
        elapsed = time.perf_counter() - t0
        assert elapsed <= 6.0, f"пересчёт {elapsed:.1f} с"
        assert res["status"] in ("optimal", "feasible", "heuristic", "partial")
        sched = {k: v for k, v in res["schedule"].items() if not v.get("kept")}
        assert verify(model, sched) == []
    with SessionLocal() as db:
        _apply_plan_from(res, StationModel(db))
        assert db.execute(text(OVERLAP_SQL)).scalar() == 0  # нет двойного резервирования после перепланирования
    return res, elapsed


def test_replan_5_incidents(world):
    res, el = _multi(world, "multi_5", 5)
    print(f"multi_5: {res['status']} solve={res['solve_ms']} мс total={el * 1000:.0f} мс changes={len(res['changes'])}")


def test_replan_10_incidents(world):
    res, el = _multi(world, "multi_10", 10)
    print(f"multi_10: {res['status']} solve={res['solve_ms']} мс total={el * 1000:.0f} мс changes={len(res['changes'])}")


def test_engine_does_not_enter_closed_track(world):
    world("normal")
    from app.services.incidents import create_incident
    with SessionLocal() as db:
        model = StationModel(db)
        arr = sorted((o for o in model.ops.values() if o.kind == "arrival" and o.status == "planned"
                      and model.track_rows[o.track_id].kind == "receiving_departure"), key=lambda o: o.planned_start)[0]
        create_incident(db, None, "track_closure", arr.track_id, duration_min=600)
        db.commit()
        delay = (aware(arr.planned_start) - model.now).total_seconds() / 60 + 5
        arr_id = arr.id
    eng = _advance(delay, steps=4)
    with SessionLocal() as db:
        o = db.get(Operation, arr_id)
        assert o.status == "planned", "заезд на закрытый путь не начинается"
        assert any("закрыт" in w for w in eng.waiting.values())


def test_engine_progresses_trains(world):
    world("normal")
    _advance(60, steps=6)
    with SessionLocal() as db:
        done = db.execute(text("SELECT count(*) FROM operations WHERE status='done'")).scalar()
        assert done > 0
        assert db.execute(text(OVERLAP_SQL)).scalar() == 0


def test_manual_reschedule_validated(client, world):
    world("normal")
    h = login(client, "station")
    with SessionLocal() as db:
        fresh_observations(db)
        model = StationModel(db)
        ops = sorted((o for o in model.ops.values() if o.kind == "arrival" and o.status == "planned"
                      and model.track_rows[o.track_id].kind == "receiving_departure"), key=lambda o: o.planned_start)
        a, b = ops[0], next(o for o in ops[1:] if o.track_id != ops[0].track_id)
        # перенос на путь другого поезда в момент его стоянки — нарушение
        r = client.post(f"/api/v1/operations/{a.id}/reschedule", headers=h,
                        json={"start": aware(b.planned_start).isoformat(), "track_id": b.track_id})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] in ("CONSTRAINTS_VIOLATED",)
    # сдвиг позже на 10 минут на том же пути — допустим, если не пересекается
    r2 = client.post(f"/api/v1/operations/{a.id}/reschedule", headers=h,
                     json={"start": (aware(a.planned_start) + timedelta(minutes=2)).isoformat()})
    assert r2.status_code in (200, 409)
    if r2.status_code == 409:
        assert r2.json()["error"]["details"]["errors"]


def test_replay_reconstructs_saved_state(world):
    world("normal")
    from app.services.hub import Hub
    from app.services.view import apply_delta
    hub = Hub()
    with SessionLocal() as db:
        fresh_observations(db)
    hub.step(False)
    for _ in range(4):
        _advance(5)
        hub.step(False)
    final = hub.state
    with SessionLocal() as db:
        snap = db.execute(select(StateSnapshot).order_by(StateSnapshot.version)).scalars().first()
        deltas = db.execute(select(StateDelta).where(StateDelta.version > snap.version).order_by(StateDelta.version)).scalars().all()
        st = snap.state
        for d in deltas:
            st = apply_delta(st, d.delta)
    norm = lambda s: json.loads(json.dumps(s, sort_keys=True, default=str))  # noqa: E731
    assert norm(st) == norm(final)
    # кольцевой буфер для восстановления соединения
    v = hub.version
    assert len(hub.catch_up(v - 2)) == 2
    assert hub.catch_up(v) == []
    assert hub.catch_up(-5) is None


def test_websocket_snapshot_and_resume(client, world):
    world("normal")
    from app.services.hub import hub
    with SessionLocal() as db:
        fresh_observations(db)
    hub.state = None
    hub.step(False)
    tok = login(client, "viewer")["Authorization"][7:]
    with client.websocket_connect(f"/ws?token={tok}") as ws:
        ws.send_text(json.dumps({"type": "hello", "last_version": -1}))
        m = json.loads(ws.receive_text())
        assert m["type"] == "snapshot" and m["version"] == hub.version
        assert "tracks" in m["state"] and m["state"]["meta"]["is_demo"]
    r = client.get("/ws")
    assert r.status_code in (400, 404, 405)


def test_index_weights_and_missing_data(world):
    world("normal")
    from app.services.index import compute_current, validate_config
    from app.models import IndexConfig
    with SessionLocal() as db:
        fresh_observations(db)
        model = StationModel(db)
        conf = detect(model)["conflicts"]
        idx = compute_current(db, model, conf)
        assert idx["value"] is not None and idx["quality"]["level"] == "full"
        cfg = db.execute(select(IndexConfig)).scalar_one()
        c = json.loads(json.dumps(cfg.config))
        c["weights"] = {k: 0.0 for k in c["weights"]}
        c["weights"]["route_conflicts"] = 1.0
        cfg.config = c
        db.commit()
        idx2 = compute_current(db, model, conf)
        assert abs(idx2["value"] - 100 * idx2["components"]["route_conflicts"]["score"]) < 0.11
        assert validate_config({**c, "weights": {k: 0 for k in c["weights"]}})
        bad = json.loads(json.dumps(c))
        bad["weights"]["idle"] = -1
        assert validate_config(bad)
    # нет актуальных данных телеметрии — составляющая исключается, а не считается «хорошей»
    with SessionLocal() as db:
        db.execute(text("UPDATE observations SET observed_at = observed_at - interval '10 minutes'"))
        db.commit()
        model = StationModel(db)
        cfg = db.execute(select(IndexConfig)).scalar_one()
        c2 = json.loads(json.dumps(cfg.config))
        c2["weights"] = {"throughput": 0.25, "schedule_deviation": 0.25, "track_utilization": 0.2, "route_conflicts": 0.15, "idle": 0.15}
        cfg.config = c2
        db.commit()
        idx3 = compute_current(db, model, [])
        assert idx3["components"]["track_utilization"]["score"] is None
        assert idx3["quality"]["level"] in ("partial", "low")


def test_assistant_answers_from_calculations(world):
    world("demo_tracks_busy")
    from app.services.assistant import answer
    with SessionLocal() as db:
        fresh_observations(db)
        a = answer(db, "Почему нельзя принять этот состав?")
        assert a["intent"] == "why_reject" and "нельзя" in a["text"] and a["facts"]
        w = answer(db, "Когда появится ближайшее окно?")
        assert "Ближайшее допустимое окно" in w["text"]
        m = answer(db, "Что произойдёт, если путь закроется?")
        assert m["missing_data"]
        q = answer(db, "Что произойдёт, если путь 4 закроется на час?")
        assert "закроется на 60 мин" in q["text"]
        o = answer(db, "Какие операции создают перегрузку?")
        assert o["intent"] == "overload"
        assert a["computed_at"] and a["based_on_version"]


def test_reports_cyrillic(client, world):
    world("normal")
    h = login(client, "viewer")
    r = client.get("/api/v1/reports/mini?format=csv&minutes=60", headers=h)
    assert r.status_code == 200
    assert r.content.startswith("﻿".encode("utf-8")) and "Мини-отчёт".encode() in r.content
    r = client.get("/api/v1/reports/mini?format=pdf&minutes=60", headers=h)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
