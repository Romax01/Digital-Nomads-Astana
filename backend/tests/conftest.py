"""Тесты выполняются на реальной PostgreSQL (база station_test) с миграциями Alembic.
Фоновый движок и MQTT отключены: шаги симуляции вызываются тестами явно."""
import os
import uuid
from datetime import timedelta

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://station:station_dev_password@db:5432/station_test")
os.environ["DATABASE_URL"] = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/station_test"
os.environ["ENGINE_ENABLED"] = "false"
os.environ["MQTT_ENABLED"] = "false"
os.environ["PLANNER_TIME_LIMIT_S"] = "3.5"
os.environ["SEED_OPTIMIZE"] = "false"  # оптимизация начального плана проверяется отдельным тестом

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def migrated():
    url = os.environ["DATABASE_URL"]
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public; CREATE EXTENSION IF NOT EXISTS btree_gist"))
    eng.dispose()
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    from app.db import reset_engine
    reset_engine(url)
    yield


def fresh_observations(db, overrides: dict | None = None, age_s: float = 0.5):
    """Актуальные показания рельсовых цепей, согласованные с учётной занятостью (как от симулятора)."""
    from app.core.timeutil import utcnow
    from app.iot.quality import _mismatch_since, expected_occupancy
    from app.models import Device, Observation
    from app.services.model import StationModel
    from sqlalchemy import select
    _mismatch_since.clear()
    model = StationModel(db, data_states={})
    occ = expected_occupancy(model)
    now = utcnow() - timedelta(seconds=age_s)
    for d in db.execute(select(Device).where(Device.kind == "track_circuit")).scalars():
        val = {"occupied": d.object_id in occ}
        if overrides and d.object_id in overrides:
            val = overrides[d.object_id]
            if val is None:
                continue
        o = db.get(Observation, (d.object_id, "occupancy", d.id))
        if o is None:
            db.add(Observation(object_id=d.object_id, attribute="occupancy", device_id=d.id, value=val,
                               observed_at=now, received_at=now, quality="good", event_id=uuid.uuid4().hex))
        else:
            o.value, o.observed_at, o.received_at, o.quality = val, now, now, "good"
        d.last_heartbeat_at = now
        d.last_seen_at = now
        d.health = {**(d.health or {}), "last_observed_at": now.isoformat()}
    db.commit()


@pytest.fixture
def world():
    """Сброс мира: world(scenario='normal', config='large', seed=42) -> None."""
    from app.db import SessionLocal
    from app.sim.seed import reset_world

    def _reset(scenario="normal", config="large", seed=42):
        with SessionLocal() as db:
            reset_world(db, config, scenario, seed)
            db.commit()
            fresh_observations(db)
    return _reset


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def login(client, username):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": "demo123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def auth(client):
    cache = {}

    def _h(username):
        if username not in cache:
            cache[username] = login(client, username)
        return cache[username]
    return _h
