"""Интеграция API с реальной тестовой БД: права, статусы, конкурентность, идемпотентность, сквозной сценарий."""
import threading
import uuid

from sqlalchemy import func, select, text

from app.db import SessionLocal
from app.models import AuditEvent, Reservation, Train, TransferRequest
from tests.conftest import fresh_observations


def _rid(number="Z-0001"):
    with SessionLocal() as db:
        return db.execute(select(TransferRequest).where(TransferRequest.number == number)).scalar_one().id


def _fresh():
    with SessionLocal() as db:
        fresh_observations(db)


def test_error_format_and_auth(client):
    r = client.get("/api/v1/requests")
    assert r.status_code == 401
    e = r.json()["error"]
    assert e["code"] == "AUTH_REQUIRED" and "вход" in e["message"].lower()
    r = client.post("/api/v1/auth/login", json={"username": "duty", "password": "wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "BAD_CREDENTIALS"


def test_validation_error_in_russian(client, world, auth):
    world("normal")
    r = client.post("/api/v1/requests", headers=auth("train"), json={"from_station_id": "OTR", "wagons_count": -5})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert any("обязательное" in f["message"] or "больше" in f["message"] for f in body["details"]["fields"])


def test_permissions_matrix_enforced_on_backend(client, world, auth):
    world("normal")
    rid = _rid()
    obs = auth("viewer")
    calls = [("post", "/api/v1/requests", {"from_station_id": "OTR", "wagons_count": 10, "wagon_kind": "gondola",
                                           "desired_departure": "2026-10-01T09:00:00Z"}),
             ("post", f"/api/v1/requests/{rid}/check", None), ("post", f"/api/v1/requests/{rid}/confirm", {}),
             ("post", f"/api/v1/requests/{rid}/cancel", {"reason": "тест"}), ("post", "/api/v1/plans/compute", None),
             ("post", "/api/v1/incidents", {"kind": "track_closure", "object_id": "ALM-T1"}),
             ("post", "/api/v1/sim/pause", None), ("put", "/api/v1/config/plan-policy", {"policy": "hard_quota"}),
             ("post", "/api/v1/observations/override", {"object_id": "ALM-T1", "occupied": True, "reason": "проверка", "valid_minutes": 5})]
    for method, url, body in calls:
        r = getattr(client, method)(url, headers=obs, json=body)
        assert r.status_code == 403, (url, r.status_code, r.text)
        assert r.json()["error"]["code"] == "FORBIDDEN"
    # поездной диспетчер не подтверждает заявки
    client.post(f"/api/v1/requests/{rid}/check", headers=auth("train"))
    r = client.post(f"/api/v1/requests/{rid}/confirm", headers=auth("train"), json={})
    assert r.status_code == 403
    assert "Дежурный по станции" in r.json()["error"]["hint"]


def test_status_transition_requires_check(client, world, auth):
    world("normal")
    rid = _rid()
    r = client.post(f"/api/v1/requests/{rid}/confirm", headers=auth("duty"), json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CHECK_REQUIRED"


def test_end_to_end_request_confirm_and_audit(client, world, auth):
    world("normal")
    _fresh()
    rid = _rid()
    r = client.post(f"/api/v1/requests/{rid}/check", headers=auth("train"))
    assert r.status_code == 200, r.text
    chk = r.json()["check"]
    assert chk["decision"].startswith("available"), chk["summary"]
    r = client.post(f"/api/v1/requests/{rid}/confirm", headers={**auth("duty"), "Idempotency-Key": uuid.uuid4().hex},
                    json={"acknowledge_warnings": True})
    assert r.status_code == 200, r.text
    tid = r.json()["train_id"]
    with SessionLocal() as db:
        req = db.get(TransferRequest, rid)
        assert req.status == "confirmed" and req.train_id == tid
        n_res = db.execute(select(func.count()).select_from(Reservation).where(Reservation.request_id == rid,
                                                                               Reservation.status == "confirmed")).scalar()
        assert n_res >= 3  # путь, стрелки, ресурсы
        actions = [a.action for a in db.execute(select(AuditEvent).where(AuditEvent.entity_id == rid)).scalars()]
        assert "request.check" in actions and "request.confirm" in actions


def test_conflicting_request_cannot_be_confirmed_by_direct_api(client, world, auth):
    world("demo_tracks_busy")
    _fresh()
    rid = _rid()
    client.post(f"/api/v1/requests/{rid}/check", headers=auth("duty"))
    r = client.post(f"/api/v1/requests/{rid}/confirm", headers=auth("duty"), json={"acknowledge_warnings": True})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "REQUEST_NOT_AVAILABLE"
    assert "Ближайшее допустимое окно" in err["message"]
    assert err["details"]["check"]["alternatives"]
    with SessionLocal() as db:
        assert db.execute(select(func.count()).select_from(Reservation).where(Reservation.request_id == rid)).scalar() == 0


def test_warnings_require_acknowledgement(client, world, auth):
    world("demo_plan_exceeded")
    _fresh()
    rid = _rid()
    client.post(f"/api/v1/requests/{rid}/check", headers=auth("duty"))
    r = client.post(f"/api/v1/requests/{rid}/confirm", headers=auth("duty"), json={})
    assert r.status_code == 409 and r.json()["error"]["code"] == "WARNINGS_NOT_ACKNOWLEDGED"
    r = client.post(f"/api/v1/requests/{rid}/confirm", headers=auth("duty"), json={"acknowledge_warnings": True})
    assert r.status_code == 200, r.text


def test_idempotent_confirm(client, world, auth):
    world("normal")
    _fresh()
    rid = _rid()
    client.post(f"/api/v1/requests/{rid}/check", headers=auth("duty"))
    key = uuid.uuid4().hex
    h = {**auth("duty"), "Idempotency-Key": key}
    r1 = client.post(f"/api/v1/requests/{rid}/confirm", headers=h, json={"acknowledge_warnings": True})
    r2 = client.post(f"/api/v1/requests/{rid}/confirm", headers=h, json={"acknowledge_warnings": True})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json().get("idempotent_replay") is True
    assert r1.json()["train_id"] == r2.json()["train_id"]
    with SessionLocal() as db:
        assert db.execute(select(func.count()).select_from(Train).where(Train.transfer_request_id == rid)).scalar() == 1
    r3 = client.post(f"/api/v1/requests/{rid}/confirm", headers=h, json={"acknowledge_warnings": False})
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_cancel_releases_reservations(client, world, auth):
    world("normal")
    _fresh()
    rid = _rid()
    client.post(f"/api/v1/requests/{rid}/check", headers=auth("duty"))
    assert client.post(f"/api/v1/requests/{rid}/confirm", headers=auth("duty"), json={"acknowledge_warnings": True}).status_code == 200
    r = client.post(f"/api/v1/requests/{rid}/cancel", headers=auth("duty"), json={"reason": "отмена отправителем"})
    assert r.status_code == 200, r.text
    assert r.json()["released_reservations"] >= 3
    with SessionLocal() as db:
        assert db.execute(select(func.count()).select_from(Reservation).where(Reservation.request_id == rid,
                                                                              Reservation.status == "confirmed")).scalar() == 0
        assert db.get(TransferRequest, rid).status == "cancelled"


def test_concurrent_confirm_no_double_booking(client, world, auth):
    """Две заявки на один момент: при параллельном подтверждении ресурсы не дублируются."""
    world("demo_track_frees")  # к прибытию свободен ровно один подходящий путь
    _fresh()
    h = auth("train")
    ids = [_rid()]
    r = client.post("/api/v1/requests", headers=h, json={"from_station_id": "OTR", "wagons_count": 40, "wagon_kind": "gondola",
                                                          "desired_departure": "2026-10-01T08:00:00Z"})
    assert r.status_code == 200, r.text
    ids.append(r.json()["id"])
    for rid in ids:
        assert client.post(f"/api/v1/requests/{rid}/check", headers=h).json()["check"]["decision"].startswith("available")
    duty = auth("duty")
    results = {}

    def go(rid):
        results[rid] = client.post(f"/api/v1/requests/{rid}/confirm", headers=duty, json={"acknowledge_warnings": True})

    th = [threading.Thread(target=go, args=(rid,)) for rid in ids]
    for t in th:
        t.start()
    for t in th:
        t.join()
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], {k: (v.status_code, v.text[:200]) for k, v in results.items()}
    with SessionLocal() as db:
        n = db.execute(text("""SELECT count(*) FROM reservations a JOIN reservations b ON a.resource_key=b.resource_key
            AND a.id < b.id AND a.status='confirmed' AND b.status='confirmed'
            AND tstzrange(a.start_at,a.end_at) && tstzrange(b.start_at,b.end_at)""")).scalar()
        assert n == 0


def test_stale_recommendation_cannot_be_applied(client, world, auth):
    world("track_closure")
    _fresh()
    r = client.post("/api/v1/incidents", headers=auth("station"), json={"kind": "track_closure", "object_id": "ALM-T4",
                                                                         "duration_min": 120})
    assert r.status_code == 200, r.text
    _fresh()
    r = client.post("/api/v1/plans/compute", headers=auth("station"))
    assert r.status_code == 200, r.text
    recs = client.get("/api/v1/recommendations", headers=auth("station")).json()
    rec = next(x for x in recs if x["status"] == "active")
    # состояние меняется после расчёта
    client.post("/api/v1/incidents", headers=auth("station"), json={"kind": "resource_failure", "object_id": "ML-2", "duration_min": 30})
    r = client.post(f"/api/v1/recommendations/{rec['id']}/apply", headers=auth("station"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "RECOMMENDATION_STALE"
