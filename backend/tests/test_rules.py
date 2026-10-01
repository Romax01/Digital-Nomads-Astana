"""Правила вместимости, месячного плана, интервальных конфликтов и три ситуации главного сценария."""
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.models import Plan, Reservation, TransferRequest
from app.services.checker import check_request
from app.services.model import IntervalBook, StationModel
from tests.conftest import fresh_observations


def _check(number="Z-0001"):
    with SessionLocal() as db:
        fresh_observations(db)
        r = db.execute(select(TransferRequest).where(TransferRequest.number == number)).scalar_one()
        return check_request(StationModel(db), r)


def items(res):
    return {i["code"]: i for i in res["items"]}


def test_situation_1_plan_exceeded_soft_then_hard(world):
    world("demo_plan_exceeded")
    res = _check()
    it = items(res)
    assert it["MONTHLY_PLAN"]["status"] == "warning", it["MONTHLY_PLAN"]
    assert res["decision"] == "available_with_warnings", res["summary"]
    assert it["MONTHLY_PLAN"]["unit"] == "вагонов" and it["MONTHLY_PLAN"]["period"]
    # жёсткая квота — отдельная настраиваемая политика
    with SessionLocal() as db:
        db.execute(select(Plan)).scalar_one().policy = "hard_quota"
        db.commit()
    res2 = _check()
    assert items(res2)["MONTHLY_PLAN"]["status"] == "fail"
    assert res2["decision"] == "unavailable"


def test_situation_2_tracks_busy_rejected_with_computed_window(world):
    world("demo_tracks_busy")
    res = _check()
    assert res["decision"] == "unavailable", res["summary"]
    assert items(res)["MONTHLY_PLAN"]["status"] == "ok"  # план не выполнен
    assert "нет подходящего свободного пути на время приёма и обработки" in res["summary"]
    assert res["nearest_window"] is not None
    assert "Ближайшее допустимое окно" in res["summary"]
    # окно рассчитано, а не записано: оно позже запрошенного прибытия и проходит полную проверку
    assert res["nearest_window"]["arrival"] > res["arrival"]
    types = {a["type"] for a in res["alternatives"]}
    assert "postpone" in types
    for a in res["alternatives"]:
        assert a["verified"] and "delay_min" in a["effect"]


def test_situation_3_full_now_frees_before_arrival(world):
    world("demo_track_frees")
    with SessionLocal() as db:
        fresh_observations(db)
        m = StationModel(db)
        book = m.build_book()
        long_rd = [t for t, r in m.track_rows.items() if r.kind == "receiving_departure" and r.useful_length_m >= 600]
        busy_now = [t for t in long_rd if book.conflicts(f"track:{t}", m.now, m.now + timedelta(minutes=1))]
        assert set(busy_now) == set(long_rd), "сейчас все подходящие пути заняты"
    res = _check()
    assert res["decision"] in ("available", "available_with_warnings"), res["summary"]
    assert res["assignment"]["tracks"][0] == "ALM-T2"
    tw = items(res)["TIME_WINDOW"]
    assert "освободится" in tw["message"]


def test_insufficient_length_data(world):
    world("normal")
    with SessionLocal() as db:
        r = db.execute(select(TransferRequest)).scalars().first()
        r.wagon_kind = None
        r.train_length_m = None
        db.commit()
    res = _check()
    assert res["decision"] in ("insufficient_data", "unavailable")
    assert items(res)["TRAIN_LENGTH"]["status"] == "insufficient_data"
    assert res["missing_data"]


def test_length_is_checked_not_wagon_count(world):
    world("normal")
    res = _check()
    track_msgs = {t["track_id"]: t for t in res["tracks"]}
    # путь 6 (560 м) короче состава 40 полувагонов (590,8 м + запас)
    assert track_msgs["ALM-T6"]["code"] == "LENGTH"


def test_interval_book_half_open():
    from datetime import datetime, timezone
    b = IntervalBook()
    t = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
    b.add("track:X", t, t + timedelta(hours=1), type="reservation")
    assert not b.conflicts("track:X", t + timedelta(hours=1), t + timedelta(hours=2))
    assert b.conflicts("track:X", t + timedelta(minutes=59), t + timedelta(hours=2))


def test_db_exclusion_constraint_blocks_overlap(world):
    world("normal")
    from app.core.timeutil import utcnow
    with SessionLocal() as db:
        now = utcnow()
        db.add(Reservation(station_id="ALM", resource_key="track:TEST", start_at=now, end_at=now + timedelta(hours=1),
                           status="confirmed", purpose="a", created_at=now))
        db.commit()
        db.add(Reservation(station_id="ALM", resource_key="track:TEST", start_at=now + timedelta(minutes=30),
                           end_at=now + timedelta(hours=2), status="confirmed", purpose="b", created_at=now))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # освобождённый резерв не мешает
        db.execute(text("UPDATE reservations SET status='released' WHERE resource_key='track:TEST'"))
        db.add(Reservation(station_id="ALM", resource_key="track:TEST", start_at=now + timedelta(minutes=30),
                           end_at=now + timedelta(hours=2), status="confirmed", purpose="c", created_at=now))
        db.commit()


def test_initial_plan_has_no_reservation_overlaps(world):
    for sc in ("normal", "peak_arrivals", "demo_tracks_busy"):
        world(sc)
        with SessionLocal() as db:
            n = db.execute(text("""SELECT count(*) FROM reservations a JOIN reservations b ON a.resource_key=b.resource_key
                AND a.id < b.id AND a.status='confirmed' AND b.status='confirmed'
                AND tstzrange(a.start_at,a.end_at) && tstzrange(b.start_at,b.end_at)""")).scalar()
            assert n == 0


def test_small_station_config_same_logic(world):
    world("normal", config="small")
    with SessionLocal() as db:
        m = StationModel(db)
        assert m.sid == "SHM"
        assert not any(t.kind == "repair" for t in m.track_rows.values()), "у малой станции нет депо"
    res = _check()
    assert res["decision"] in ("available", "available_with_warnings", "unavailable")
    assert res["items"]
