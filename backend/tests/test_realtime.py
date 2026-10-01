"""Режим реального времени: старт от текущего момента, ход по часам сервера, пополнение расписания."""
from datetime import timedelta

from sqlalchemy import func, select

from app.core.timeutil import aware, utcnow
from app.db import SessionLocal
from app.models import Reservation, SimState, Train
from tests.conftest import fresh_observations


def _reset_rt(config="large"):
    from app.sim.seed import reset_world
    with SessionLocal() as db:
        reset_world(db, config, "normal", 42, real_time=True)
        db.commit()
        fresh_observations(db)


def test_reset_starts_now_and_runs_at_wall_clock():
    _reset_rt()
    with SessionLocal() as db:
        s = db.get(SimState, 1)
        assert abs((aware(s.model_time) - utcnow()).total_seconds()) < 120
        assert s.running and s.speed == 1.0 and s.world["real_time"]
        assert db.execute(select(func.count()).select_from(Train)).scalar() > 10
    from app.sim.engine import Engine
    eng = Engine()
    eng.step(advance=True)
    with SessionLocal() as db:
        assert abs((aware(db.get(SimState, 1).model_time) - utcnow()).total_seconds()) < 5


def test_timetable_is_extended_without_double_booking():
    _reset_rt()
    from app.sim.seed import extend_timetable
    with SessionLocal() as db:
        s = db.get(SimState, 1)
        before = db.execute(select(func.count()).select_from(Train)).scalar()
        ids_before = {t for (t,) in db.execute(select(Train.id))}
        until = __import__("datetime").datetime.fromisoformat(s.world["tt_until"])
        # конец расписания близко: добавляется следующее окно
        added = extend_timetable(db, s, until - timedelta(hours=1))
        db.commit()
        assert added > 0
        after = {t for (t,) in db.execute(select(Train.id))}
        assert len(after) == before + added and ids_before <= after
        new_until = __import__("datetime").datetime.fromisoformat(db.get(SimState, 1).world["tt_until"])
        assert new_until - until == timedelta(hours=4)
        # пересечений подтверждённых резервов нет (ограничение-исключение PostgreSQL + проверка)
        rs = db.execute(select(Reservation).where(Reservation.status == "confirmed")).scalars().all()
        by = {}
        for r in rs:
            by.setdefault(r.resource_key, []).append((aware(r.start_at), aware(r.end_at)))
        for k, iv in by.items():
            iv.sort()
            for (a0, a1), (b0, b1) in zip(iv, iv[1:]):
                assert b0 >= a1, k
        # пока до конца расписания далеко — ничего не добавляется
        assert extend_timetable(db, db.get(SimState, 1), until - timedelta(hours=1)) == 0


def test_pause_or_speed_leaves_real_time(client, auth):
    _reset_rt()
    h = auth("admin")
    assert client.get("/api/v1/state", headers=h).status_code in (200, 503)
    r = client.post("/api/v1/sim/speed", headers=h, json={"speed": 5})
    assert r.status_code == 200
    with SessionLocal() as db:
        assert not db.get(SimState, 1).world.get("real_time")
    r = client.post("/api/v1/sim/reset", headers=h, json={"scenario": "normal", "seed": 42, "real_time": True})
    assert r.status_code == 200 and r.json()["real_time"] is True
    client.post("/api/v1/sim/pause", headers=h)
    with SessionLocal() as db:
        s = db.get(SimState, 1)
        assert not s.running and not s.world.get("real_time")
