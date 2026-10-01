"""Приём телеметрии: повторы, порядок, перезапуск, retained, единицы, неизвестные устройства,
противоречия, потеря heartbeat, блокировка операций при недостоверных данных."""
import json
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.core.timeutil import utcnow
from app.db import SessionLocal
from app.iot.ingest import IngestProcessor, RawItem
from app.iot.quality import device_statuses, track_data_states
from app.models import Device, Observation, TelemetryEvent, TelemetryReject, TransferRequest
from app.services.checker import check_request
from app.services.model import StationModel
from tests.conftest import fresh_observations


def msg(device="ALM-TC-3", obj="ALM-T3", etype="occupancy", payload=None, seq=1, boot="b1", observed=None,
        station="ALM", schema="1.0", event_id=None):
    return {"schema_version": schema, "event_id": event_id or uuid.uuid4().hex, "device_id": device, "station_id": station,
            "object_id": obj, "event_type": etype, "observed_at": (observed or utcnow()).isoformat(),
            "sequence_number": seq, "boot_id": boot, "payload": payload if payload is not None else {"occupied": False},
            "quality": "good", "source_mode": "simulator"}


def push(proc, m, kind="track_circuit", retained=False):
    topic = f"station/{m['station_id']}/{kind}/{m['device_id']}"
    with SessionLocal() as db:
        res = proc.process_batch(db, [RawItem(topic=topic, payload=json.dumps(m).encode(), retained=retained)])
        db.commit()
    return res


def obs_value(obj="ALM-T3", dev="ALM-TC-3"):
    with SessionLocal() as db:
        o = db.get(Observation, (obj, "occupancy", dev))
        return o.value if o else None


def test_duplicate_message_no_repeat_action(world):
    world("normal")
    p = IngestProcessor()
    m = msg(seq=10)
    assert push(p, m).applied == 1
    r = push(IngestProcessor(), m)  # новый процесс (после перезапуска) — дубль отсекается по БД
    assert r.duplicates == 1 and r.applied == 0
    with SessionLocal() as db:
        assert db.execute(select(func.count()).select_from(TelemetryEvent).where(TelemetryEvent.event_id == m["event_id"])).scalar() == 1


def test_out_of_order_kept_in_history_not_applied(world):
    world("normal")
    p = IngestProcessor()
    t = utcnow()
    push(p, msg(seq=5, payload={"occupied": True}, observed=t))
    r = push(p, msg(seq=4, payload={"occupied": False}, observed=t - timedelta(seconds=1)))
    assert r.late == 1
    assert obs_value() == {"occupied": True}
    with SessionLocal() as db:
        assert db.execute(select(TelemetryEvent).where(TelemetryEvent.sequence_number == 4)).scalar_one().disposition == "late"


def test_device_reboot_resets_counter(world):
    world("normal")
    p = IngestProcessor()
    push(p, msg(seq=100, boot="boot-A", payload={"occupied": True}))
    r = push(p, msg(seq=1, boot="boot-B", payload={"occupied": False}))
    assert r.applied == 1 and obs_value() == {"occupied": False}
    with SessionLocal() as db:
        d = db.get(Device, "ALM-TC-3")
        assert d.last_boot_id == "boot-B" and d.last_seq == 1


def test_stale_retained_message_does_not_refresh(world):
    world("normal")
    with SessionLocal() as db:
        before = db.get(Observation, ("ALM-T3", "occupancy", "ALM-TC-3")).observed_at
    p = IngestProcessor()
    r = push(p, msg(seq=1, observed=utcnow() - timedelta(minutes=5), payload={"occupied": True}), retained=True)
    assert r.stale == 1 and r.applied == 0
    with SessionLocal() as db:
        o = db.get(Observation, ("ALM-T3", "occupancy", "ALM-TC-3"))
        assert o.observed_at == before  # старое retained-сообщение не «освежило» данные
        ev = db.execute(select(TelemetryEvent).where(TelemetryEvent.device_id == "ALM-TC-3")).scalars().first()
        assert ev.disposition == "stale"  # но сохранено в истории


def test_bad_unit_and_range_rejected(world):
    world("normal")
    p = IngestProcessor()
    m = msg(device="ALM-GPS-ML-1", obj="ML-1", etype="position",
            payload={"x": {"value": 10, "unit": "m"}, "y": {"value": 5, "unit": "m"}, "speed": {"value": 20, "unit": "mph"}})
    r = push(p, m, kind="loco_gps")
    assert r.rejected == 1
    m2 = msg(device="ALM-GPS-ML-1", obj="ML-1", etype="position",
             payload={"x": {"value": 10, "unit": "m"}, "y": {"value": 5, "unit": "m"}, "speed": {"value": 500, "unit": "km/h"}})
    assert push(p, m2, kind="loco_gps").rejected == 1
    m3 = msg(device="ALM-GPS-ML-1", obj="ML-1", etype="position",
             payload={"x": {"value": 10, "unit": "m"}, "y": {"value": 5, "unit": "m"}, "speed": {"value": 5, "unit": "m/s"}})
    assert push(p, m3, kind="loco_gps").applied == 1
    with SessionLocal() as db:
        codes = [r.reason_code for r in db.execute(select(TelemetryReject)).scalars()]
        assert "BAD_UNIT" in codes and "OUT_OF_RANGE" in codes
        pos = db.get(Observation, ("ML-1", "position", "ALM-GPS-ML-1"))
        assert pos.value["speed"] == 18.0  # 5 м/с → 18 км/ч


def test_unknown_device_schema_station_rejected(world):
    world("normal")
    p = IngestProcessor()
    assert push(p, msg(device="ALM-TC-99")).rejected == 1
    assert push(p, msg(schema="9.9")).rejected == 1
    assert push(p, msg(etype="position", payload={})).rejected == 1  # тип не разрешён устройству
    assert push(p, msg(obj="ALM-T4")).rejected == 1                  # объект не связан с устройством
    with SessionLocal() as db:
        codes = {r.reason_code for r in db.execute(select(TelemetryReject)).scalars()}
        assert {"UNKNOWN_DEVICE", "UNKNOWN_SCHEMA", "EVENT_NOT_ALLOWED", "OBJECT_MISMATCH"} <= codes


def test_stale_data_blocks_confirmation_then_recovers(world):
    world("demo_track_frees")
    with SessionLocal() as db:
        # датчик пути 2 (единственного, который освободится) перестал передавать данные
        fresh_observations(db, age_s=0.5)
        o = db.get(Observation, ("ALM-T2", "occupancy", "ALM-TC-2"))
        o.observed_at = o.received_at = utcnow() - timedelta(seconds=18)
        db.commit()
        model = StationModel(db)
        ds = model.data_states["ALM-T2"]
        assert ds["state"] == "stale"
        assert "устарели" in ds["message"] and "18 секунд назад" in ds["message"]
        r = db.execute(select(TransferRequest)).scalars().first()
        res = check_request(model, r)
        assert res["decision"] == "unavailable"
        t2 = next(t for t in res["tracks"] if t["track_id"] == "ALM-T2")
        assert t2["code"] == "DATA"
    with SessionLocal() as db:
        fresh_observations(db)
        r = db.execute(select(TransferRequest)).scalars().first()
        res = check_request(StationModel(db), r)
        assert res["decision"].startswith("available")


def test_contradiction_detected_after_grace(world):
    import time as _t
    from app.iot import quality
    world("demo_track_frees")
    with SessionLocal() as db:
        fresh_observations(db, overrides={"ALM-T1": {"occupied": False}})  # на пути 1 стоит поезд по журналу
        m = StationModel(db, data_states={})
        st = track_data_states(db, utcnow(), m)["ALM-T1"]
        assert st["state"] == "actual"  # в пределах окна задержки доставки
        quality._mismatch_since["ALM-T1"] = _t.monotonic() - 10
        st = track_data_states(db, utcnow(), m)["ALM-T1"]
        assert st["state"] == "contradictory"
        assert "Сохранены оба наблюдения" in st["message"]


def test_heartbeat_loss_marks_device_offline(world):
    world("normal")
    with SessionLocal() as db:
        fresh_observations(db)
        d = db.get(Device, "ALM-TC-3")
        d.last_heartbeat_at = utcnow() - timedelta(seconds=60)
        db.commit()
        st = {x["id"]: x for x in device_statuses(db, utcnow())}
        assert st["ALM-TC-3"]["connection"] == "offline"
        assert st["ALM-TC-1"]["connection"] == "online"


def test_high_rate_stream_and_queue_overflow():
    from app.iot.runtime import IngestQueue
    q = IngestQueue(maxlen=100)
    for i in range(150):
        q.put(RawItem(topic="station/ALM/track_circuit/ALM-TC-1", payload=json.dumps(msg(seq=i)).encode()))
    for i in range(500):  # частые координаты объединяются
        q.put(RawItem(topic="station/ALM/loco_gps/ALM-GPS-ML-1",
                      payload=json.dumps({"event_type": "position", "n": i}).encode()))
    d = q.depth()
    assert d["significant"] == 100 and d["dropped"] == 50 and d["coordinates"] == 1
    batch = q.take(max_items=1000)
    assert batch[0].topic.endswith("ALM-TC-1")  # значимые раньше координат
