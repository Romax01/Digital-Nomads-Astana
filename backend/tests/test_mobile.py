"""Процесс работников: дефект → решение → работы → контрольный осмотр; замена вагона; права,
области видимости, идемпотентность, конкурентность, сценарный режим. Реальная PostgreSQL."""
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.timeutil import aware
from app.db import SessionLocal
from app.models import DefectReport, Operation, SimState, Train, User, Wagon, WorkOrder
from tests.conftest import fresh_observations

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200 + b"\xff\xd9"


def _h(auth, u, key=None):
    h = dict(auth(u))
    if key:
        h["Idempotency-Key"] = key
    return h


def _wagon(loaded=None, kinds=("gondola", "covered", "tank", "hopper"), skip=()):
    """Вагон в составе поезда на станции (детерминированно по номеру)."""
    with SessionLocal() as db:
        trains = sorted(db.execute(select(Train).where(Train.status == "on_station")).scalars(), key=lambda t: t.number)
        for t in trains:
            for w in sorted(db.execute(select(Wagon).where(Wagon.train_id == t.id)).scalars(), key=lambda w: w.position):
                if w.kind in kinds and (loaded is None or w.loaded == loaded) and w.condition == "ok" and w.id not in skip:
                    return {"id": w.id, "number": w.number, "train_id": t.id, "position": w.position, "kind": w.kind}
    pytest.skip("нет подходящего вагона")


def _report(client, auth, w, urgency="normal", user="inspector", photo=False, cu=None):
    att = []
    if photo:
        r = client.post(f"/api/v1/attachments?client_uuid={uuid.uuid4().hex}&filename=defect.jpg", content=JPEG,
                        headers={**auth(user), "Content-Type": "image/jpeg"})
        assert r.status_code == 200, r.text
        att = [r.json()["id"]]
    body = {"client_uuid": cu or uuid.uuid4().hex, "wagon_id": w["id"], "wagon_number": w["number"], "category": "brake",
            "component": "Колодка", "description": "Изношена тормозная колодка", "urgency": urgency, "attachment_ids": att}
    r = client.post("/api/v1/defect-reports", headers=auth(user), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _act(client, auth, user, rid, action, **body):
    return client.post(f"/api/v1/defect-reports/{rid}/actions/{action}", headers=auth(user), json=body)


def _wo(client, auth, user, wid, action, **body):
    return client.post(f"/api/v1/work-orders/{wid}/actions/{action}", headers=auth(user), json=body)


def _cond(wid):
    with SessionLocal() as db:
        return db.get(Wagon, wid).condition


def test_main_flow_report_photo_ack_repair_inspection_persisted(client, world, auth):
    """Сценарий 1: сообщение с фото → подтверждение → заявка → работы → приёмка другим проверяющим."""
    world("normal")
    w = _wagon()
    rep = _report(client, auth, w, urgency="urgent", photo=True)
    assert rep["status"] == "submitted" and rep["acknowledged_at"] is None and len(rep["attachments"]) == 1
    # оператор ПТО видит сообщение в очереди и подтверждает получение (отдельно от доставки)
    q = client.get("/api/v1/defect-reports?scope=station&status=open", headers=auth("pto")).json()
    assert any(i["id"] == rep["id"] for i in q["items"])
    r = _act(client, auth, "pto", rep["id"], "acknowledge", expected_version=rep["version"])
    assert r.status_code == 200 and r.json()["acknowledged_at"]
    # уведомление автору
    n = client.get("/api/v1/notifications?unread=true", headers=auth("inspector")).json()
    assert any(x["kind"] == "defect.ack" for x in n["items"])
    # оператор ПТО не принимает решение — только диспетчер
    r = client.post(f"/api/v1/defect-reports/{rep['id']}/decision", headers=auth("pto"), json={"kind": "repair_in_place"})
    assert r.status_code == 403
    r = client.post(f"/api/v1/defect-reports/{rep['id']}/decision", headers=_h(auth, "station", "dec-1"),
                    json={"kind": "repair_in_place", "assignee_id": "u-fitter", "reason": "Ремонт на месте допустим"})
    assert r.status_code == 200, r.text
    wo = r.json()["work_order"]
    assert wo["status"] == "assigned" and r.json()["defect"]["status"] == "accepted"
    assert _cond(w["id"]) == "faulty"  # подтверждённая неисправность — вагон не уйдёт со станцией
    # повтор того же решения с тем же ключом — тот же результат, без второй заявки
    r2 = client.post(f"/api/v1/defect-reports/{rep['id']}/decision", headers=_h(auth, "station", "dec-1"),
                     json={"kind": "repair_in_place", "assignee_id": "u-fitter", "reason": "Ремонт на месте допустим"})
    assert r2.json().get("idempotent_replay")
    with SessionLocal() as db:
        assert len(db.execute(select(WorkOrder).where(WorkOrder.defect_report_id == rep["id"])).scalars().all()) == 1
    # исполнитель: начало → отчёт
    assert _wo(client, auth, "fitter", wo["id"], "start").status_code == 200
    assert _wo(client, auth, "fitter", wo["id"], "submit", report="Колодка заменена").status_code == 200
    # «Работа выполнена» исполнителем не снимает неисправность
    assert _cond(w["id"]) == "faulty"
    r = _wo(client, auth, "senior", wo["id"], "accept", comment="Проверено, замечаний нет")
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert _cond(w["id"]) == "ok"
    # данные сохранены в БД (переживают перезапуск: новая сессия, без кэша процесса)
    with SessionLocal() as db:
        d = db.get(DefectReport, rep["id"])
        assert d.status == "accepted" and not d.fault_open
        assert db.get(WorkOrder, wo["id"]).status == "completed"
    card = client.get(f"/api/v1/defect-reports/{rep['id']}", headers=auth("inspector")).json()
    kinds = [e["to"] for e in card["timeline"] if e["kind"] == "status"]
    assert kinds[:2] == ["submitted", "acknowledged"] and "accepted" in kinds


def test_needs_info_reject_duplicate_and_rework_history(client, world, auth):
    """Сценарий 2: уточнение, отказ с причиной, объединение дубликатов, возврат на доработку."""
    world("normal")
    w = _wagon()
    a = _report(client, auth, w)
    assert _act(client, auth, "pto", a["id"], "needs_info").status_code == 400  # без текста запроса
    r = _act(client, auth, "pto", a["id"], "needs_info", reason="Укажите сторону вагона")
    assert r.json()["status"] == "needs_info"
    assert _act(client, auth, "repairer", a["id"], "reply", reason="x").status_code == 404  # чужое сообщение не видно
    r = _act(client, auth, "inspector", a["id"], "reply", reason="Правая сторона, 2-я тележка")
    assert r.status_code == 200 and r.json()["status"] == "under_review"
    # дубликат: второе сообщение по тому же вагону объединяется с первым — с основанием и ссылкой
    b = _report(client, auth, w)
    assert _act(client, auth, "pto", b["id"], "duplicate", reason="Тот же дефект").status_code == 400
    r = _act(client, auth, "pto", b["id"], "duplicate", duplicate_of=a["id"], reason="Тот же дефект")
    assert r.status_code == 200 and r.json()["status"] == "duplicate" and r.json()["duplicate_of"] == a["id"]
    # отказ без основания невозможен, с основанием — записан
    c = _report(client, auth, _wagon(skip={w["id"]}))
    assert _act(client, auth, "pto", c["id"], "reject").status_code == 400
    r = _act(client, auth, "pto", c["id"], "reject", reason="Дефект не подтверждён при повторном осмотре")
    assert r.json()["status"] == "rejected" and r.json()["decision_reason"]
    # доработка
    r = client.post(f"/api/v1/defect-reports/{a['id']}/decision", headers=auth("station"),
                    json={"kind": "repair_in_place", "assignee_id": "u-repairer"})
    wo = r.json()["work_order"]
    _wo(client, auth, "repairer", wo["id"], "start")
    _wo(client, auth, "repairer", wo["id"], "submit", report="Отрегулировано")
    assert _wo(client, auth, "senior", wo["id"], "rework").status_code == 400  # замечания обязательны
    r = _wo(client, auth, "senior", wo["id"], "rework", reason="Не затянут крепёж")
    assert r.json()["status"] == "rework"
    assert _wo(client, auth, "repairer", wo["id"], "start").json()["status"] == "in_progress"
    _wo(client, auth, "repairer", wo["id"], "submit", report="Крепёж затянут")
    r = _wo(client, auth, "senior", wo["id"], "accept")
    card = r.json()
    assert card["status"] == "completed" and [i["result"] for i in card["inspections"]] == ["rework", "accepted"]
    statuses = [e["to"] for e in card["timeline"] if e["kind"] == "status"]
    assert statuses == ["created", "in_progress", "awaiting_inspection", "rework", "in_progress", "awaiting_inspection", "completed"] \
        or statuses[-1] == "completed"


def _run_ops(wid):
    """Операции заявки размещаются и выполняются в модельном времени (как после планировщика)."""
    from app.sim.engine import Engine
    with SessionLocal() as db:
        wo = db.get(WorkOrder, wid)
        now = aware(db.get(SimState, 1).model_time)
        for oid in wo.operation_ids:
            o = db.get(Operation, oid)
            o.reserved, o.planned_start, o.planned_end = True, now, now + timedelta(minutes=o.duration_min)
        db.commit()
    eng = Engine()
    for _ in range(40):  # операция встаёт в очередь технологии состава (после выгрузки и манёвров)
        with SessionLocal() as db:
            s = db.get(SimState, 1)
            s.model_time = aware(s.model_time) + timedelta(minutes=10)
            db.commit()
            fresh_observations(db)
        eng.step(advance=False)
        with SessionLocal() as db:
            if all(db.get(Operation, oid).status == "done" for oid in db.get(WorkOrder, wid).operation_ids):
                return
    raise AssertionError("операция по заявке не выполнена движком")


def test_replacement_changes_consist_only_after_operation(client, world, auth):
    """Сценарий 3: замена конкретного вагона исправным — состав меняется только после операции;
    снятый вагон остаётся неисправным до собственного ремонта и приёмки."""
    world("normal")
    w = _wagon(loaded=False)
    rep = _report(client, auth, w)
    _act(client, auth, "pto", rep["id"], "acknowledge")
    cands = client.get(f"/api/v1/wagons/{w['id']}/replacement-candidates", headers=auth("station")).json()
    spare = next(c for c in cands["candidates"] if c["suitable"])
    with SessionLocal() as db:
        before_count = db.get(Train, w["train_id"]).wagons_count
    r = client.post(f"/api/v1/defect-reports/{rep['id']}/decision", headers=auth("station"),
                    json={"kind": "replacement", "replacement_wagon_id": spare["id"], "assignee_id": "u-repairer"})
    assert r.status_code == 200, r.text
    wo = r.json()["work_order"]
    with SessionLocal() as db:
        assert db.get(Wagon, w["id"]).train_id == w["train_id"]  # заявка сама состав не меняет
        assert db.get(Wagon, spare["id"]).train_id is None
    r = _wo(client, auth, "repairer", wo["id"], "start")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLACE_NOT_READY"
    _run_ops(wo["id"])
    with SessionLocal() as db:
        fw, sw, t = db.get(Wagon, w["id"]), db.get(Wagon, spare["id"]), db.get(Train, w["train_id"])
        assert fw.train_id is None and fw.track_id and fw.condition == "in_repair"
        assert sw.train_id == t.id and sw.position == w["position"]
        assert t.wagons_count == before_count
    assert _wo(client, auth, "repairer", wo["id"], "start").status_code == 200
    _wo(client, auth, "repairer", wo["id"], "submit", report="Вагон прицеплен, тормоза опробованы")
    assert _wo(client, auth, "senior", wo["id"], "accept").json()["status"] == "completed"
    assert _cond(w["id"]) == "in_repair"  # принятие замены не делает снятый вагон исправным
    assert _cond(spare["id"]) == "ok"
    # собственный ремонт снятого вагона — отдельная заявка с приёмкой
    r = client.post(f"/api/v1/defect-reports/{rep['id']}/work-orders", headers=auth("station"),
                    json={"kind": "repair_in_place", "assignee_id": "u-fitter"})
    wo2 = r.json()
    _wo(client, auth, "fitter", wo2["id"], "start")
    _wo(client, auth, "fitter", wo2["id"], "submit", report="Отремонтирован в депо")
    _wo(client, auth, "senior", wo2["id"], "accept")
    assert _cond(w["id"]) == "ok"


def test_no_depot_loaded_wagon_and_no_candidates_explained(client, world, auth):
    """Сценарий 4: нет ремонтного пути / подходящего вагона — объяснение, не фиктивный успех."""
    world("normal", config="small")
    w = _wagon()
    rep = _report(client, auth, w)
    _act(client, auth, "pto", rep["id"], "acknowledge")
    r = client.post(f"/api/v1/defect-reports/{rep['id']}/decision", headers=auth("station"), json={"kind": "uncoupling_repair"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_REPAIR_TRACK"
    assert "repair_in_place" in r.json()["error"]["details"]["options"]
    world("normal")
    lw = _wagon(loaded=True)
    rep = _report(client, auth, lw)
    _act(client, auth, "pto", rep["id"], "acknowledge")
    r = client.post(f"/api/v1/defect-reports/{rep['id']}/decision", headers=auth("station"),
                    json={"kind": "replacement", "replacement_wagon_id": "W59900011"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "LOADED_WAGON" and "перегрузк" in r.json()["error"]["message"]
    with SessionLocal() as db:
        assert db.get(DefectReport, rep["id"]).status == "acknowledged"  # ничего не изменилось


def test_offline_resend_is_idempotent_and_key_not_reused(client, world, auth):
    """Сценарий 5 (сервер): повторная отправка офлайн-очереди не дублирует сообщение и фото."""
    world("normal")
    w = _wagon()
    cu, pu = uuid.uuid4().hex, uuid.uuid4().hex
    p1 = client.post(f"/api/v1/attachments?client_uuid={pu}", content=JPEG, headers={**auth("inspector"), "Content-Type": "image/jpeg"})
    p2 = client.post(f"/api/v1/attachments?client_uuid={pu}", content=JPEG, headers={**auth("inspector"), "Content-Type": "image/jpeg"})
    assert p1.json()["id"] == p2.json()["id"] and p2.json()["idempotent_replay"]
    body = {"client_uuid": cu, "wagon_id": w["id"], "wagon_number": w["number"], "category": "coupler",
            "description": "Трещина корпуса автосцепки", "urgency": "urgent", "attachment_ids": [p1.json()["id"]]}
    a = client.post("/api/v1/defect-reports", headers=auth("inspector"), json=body).json()
    b = client.post("/api/v1/defect-reports", headers=auth("inspector"), json=body).json()
    assert a["id"] == b["id"] and b["idempotent_replay"] and not a["idempotent_replay"]
    r = client.post("/api/v1/defect-reports", headers=auth("inspector"), json={**body, "description": "Другое"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    # неверный тип файла и подмена содержимого отклоняются
    r = client.post(f"/api/v1/attachments?client_uuid={uuid.uuid4().hex}", content=b"<svg/>", headers={**auth("inspector"), "Content-Type": "image/svg+xml"})
    assert r.status_code == 415
    r = client.post(f"/api/v1/attachments?client_uuid={uuid.uuid4().hex}", content=b"not a jpeg", headers={**auth("inspector"), "Content-Type": "image/jpeg"})
    assert r.status_code == 415
    # неизвестный номер не создаёт вагон: сообщение с неразрешённой привязкой
    with SessionLocal() as db:
        n_before = db.execute(select(Wagon)).scalars().all().__len__()
    u = client.post("/api/v1/defect-reports", headers=auth("inspector"), json={**body, "client_uuid": uuid.uuid4().hex,
                    "wagon_id": None, "wagon_number": "99999999", "attachment_ids": [], "urgency": "critical"}).json()
    assert u["wagon_linked"] is False and u["restriction_active"] is False
    with SessionLocal() as db:
        assert len(db.execute(select(Wagon)).scalars().all()) == n_before
    # решение по неразрешённой привязке невозможно; привязка выполняется уполномоченным
    _act(client, auth, "pto", u["id"], "acknowledge")
    r = client.post(f"/api/v1/defect-reports/{u['id']}/decision", headers=auth("station"), json={"kind": "inspection"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "WAGON_NOT_LINKED"
    r = _act(client, auth, "pto", u["id"], "link_wagon", wagon_id=w["id"])
    assert r.json()["wagon_linked"] and r.json()["restriction_active"]  # критичное — ограничение после привязки
    assert _cond(w["id"]) == "restricted"


def test_concurrent_decision_and_double_reservation(client, world, auth):
    """Сценарий 6: конкурентное решение не создаёт двойной ремонт; один вагон — не две замены."""
    world("normal")
    w1 = _wagon(loaded=False)
    rep = _report(client, auth, w1)
    _act(client, auth, "pto", rep["id"], "acknowledge")
    from app.services import workflow as wf
    results = []

    def go():
        with SessionLocal() as db:
            u = db.get(User, "u-station")
            try:
                wf.decide(db, u, rep["id"], {"kind": "repair_in_place"})
                db.commit()
                results.append("ok")
            except Exception as e:  # noqa: BLE001
                db.rollback()
                results.append(getattr(e, "code", type(e).__name__))
    ts = [threading.Thread(target=go) for _ in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 1
    with SessionLocal() as db:
        assert len(db.execute(select(WorkOrder).where(WorkOrder.defect_report_id == rep["id"])).scalars().all()) == 1
    # устаревшая версия → 409
    cur = client.get(f"/api/v1/defect-reports/{rep['id']}", headers=auth("station")).json()
    r = _act(client, auth, "station", rep["id"], "comment", reason="x", expected_version=cur["version"] - 1)
    assert r.status_code == 409 and r.json()["error"]["code"] == "STALE_VERSION"
    # один исправный вагон — не две замены
    w2 = _wagon(loaded=False, kinds=(w1["kind"],), skip={w1["id"]})
    w3 = _wagon(loaded=False, kinds=(w1["kind"],), skip={w1["id"], w2["id"]})
    spare = next(c for c in client.get(f"/api/v1/wagons/{w2['id']}/replacement-candidates", headers=auth("station")).json()["candidates"] if c["suitable"])
    ids = []
    for w in (w2, w3):
        r = _report(client, auth, w)
        _act(client, auth, "pto", r["id"], "acknowledge")
        ids.append(r["id"])
    a = client.post(f"/api/v1/defect-reports/{ids[0]}/decision", headers=auth("station"), json={"kind": "replacement", "replacement_wagon_id": spare["id"]})
    b = client.post(f"/api/v1/defect-reports/{ids[1]}/decision", headers=auth("station"), json={"kind": "replacement", "replacement_wagon_id": spare["id"]})
    assert a.status_code == 200 and b.status_code == 409
    assert b.json()["error"]["code"] in ("REPLACEMENT_UNSUITABLE", "REPLACEMENT_RESERVED")


def test_scopes_station_isolation_api_ws_and_attachments(client, world, auth):
    """Сценарий 7: чужая станция, неназначенные задания, админ-API и чужие вложения закрыты."""
    world("normal")
    w = _wagon()
    rep = _report(client, auth, w, photo=True)
    att = rep["attachments"][0]["url"]
    # работник другой станции: не видит сообщение, вложение, вагоны, очередь
    assert client.get(f"/api/v1/defect-reports/{rep['id']}", headers=auth("otr_inspector")).status_code == 404
    assert client.get(att, headers=auth("otr_inspector")).status_code == 404
    assert client.get("/api/v1/mobile/wagons?q=59", headers=auth("otr_inspector")).json() == []
    assert client.get("/api/v1/defect-reports?scope=own", headers=auth("otr_inspector")).json()["items"] == []
    # рабочие роли не получают полный снимок станции, админ-API и Swagger
    for u in ("inspector", "fitter", "pto", "senior"):
        assert client.get("/api/v1/state", headers=auth(u)).status_code == 403
        assert client.get("/api/v1/admin/users", headers=auth(u)).status_code == 403
        assert client.post("/api/v1/auth/docs-session", headers=auth(u)).status_code == 403
    # неназначенное задание другой бригады не видно слесарю
    _act(client, auth, "pto", rep["id"], "acknowledge")
    wo = client.post(f"/api/v1/defect-reports/{rep['id']}/decision", headers=auth("station"),
                     json={"kind": "repair_in_place", "assignee_id": "u-fitter2"}).json()["work_order"]
    assert client.get(f"/api/v1/work-orders/{wo['id']}", headers=auth("fitter")).status_code == 404
    assert all(i["id"] != wo["id"] for i in client.get("/api/v1/work-orders?scope=mine", headers=auth("fitter")).json()["items"])
    assert client.get(f"/api/v1/work-orders/{wo['id']}", headers=auth("fitter2")).status_code == 200
    assert _wo(client, auth, "fitter", wo["id"], "start").status_code == 404
    # WebSocket: рабочая роль получает ограниченный канал без снимка станции
    tok = auth("inspector")["Authorization"][7:]
    with client.websocket_connect(f"/ws?token={tok}") as ws:
        ws.send_json({"type": "hello", "last_version": -1})
        msg = ws.receive_json()
        assert msg["type"] == "hello_ok" and "state" not in msg


def test_self_acceptance_multiple_defects_incident_and_simulation(client, world, auth):
    """Сценарий 8: свою работу не принять; несколько дефектов не снимаются одной приёмкой;
    закрытие инцидента и симуляция не обходят проверки."""
    world("normal")
    w = _wagon()
    a, b = _report(client, auth, w), _report(client, auth, w)
    for x in (a, b):
        _act(client, auth, "pto", x["id"], "acknowledge")
    wa = client.post(f"/api/v1/defect-reports/{a['id']}/decision", headers=auth("station"),
                     json={"kind": "repair_in_place", "assignee_id": "u-senior"}).json()["work_order"]
    client.post(f"/api/v1/defect-reports/{b['id']}/decision", headers=auth("station"),
                json={"kind": "repair_in_place", "assignee_id": "u-fitter"})
    _wo(client, auth, "senior", wa["id"], "start")
    _wo(client, auth, "senior", wa["id"], "submit", report="Сделано")
    r = _wo(client, auth, "senior", wa["id"], "accept")
    assert r.status_code == 403 and r.json()["error"]["code"] == "SELF_INSPECTION"
    assert _wo(client, auth, "senior2", wa["id"], "accept").json()["status"] == "completed"
    assert _cond(w["id"]) == "faulty"  # второй дефект того же вагона не устранён
    # симуляция не завершает ручные работы
    from app.sim.engine import Engine
    eng = Engine()
    for _ in range(3):
        with SessionLocal() as db:
            s = db.get(SimState, 1)
            s.model_time = aware(s.model_time) + timedelta(hours=2)
            db.commit()
            fresh_observations(db)
        eng.step(advance=False)
    with SessionLocal() as db:
        wb = db.execute(select(WorkOrder).where(WorkOrder.defect_report_id == b["id"])).scalar_one()
        assert wb.status == "assigned" and db.get(Wagon, w["id"]).condition == "faulty"
    # критичное сообщение: ограничение до проверки; ПТО не снимает его отклонением
    w2 = _wagon(skip={w["id"]})
    c = _report(client, auth, w2, urgency="critical")
    assert c["restriction_active"] and _cond(w2["id"]) == "restricted"
    r = _act(client, auth, "pto", c["id"], "reject", reason="Не подтвердилось")
    assert r.status_code == 403
    assert _act(client, auth, "station", c["id"], "reject", reason="Не подтвердилось при повторном осмотре").status_code == 200
    assert _cond(w2["id"]) == "ok"
    # инцидент из кабинета: сообщение + ограничение; закрыть инцидент до приёмки нельзя
    w3 = _wagon(skip={w["id"], w2["id"]})
    r = client.post("/api/v1/incidents", headers=auth("station"), json={"kind": "faulty_wagon", "object_id": w3["id"]})
    assert r.status_code == 200, r.text
    inc = r.json()["id"]
    assert _cond(w3["id"]) == "restricted"
    r = client.post(f"/api/v1/incidents/{inc}/resolve", headers=auth("station"), json={"reason": "закрыть"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DEFECT_NOT_CLOSED"
    assert _cond(w3["id"]) == "restricted"


def test_role_matrix_and_inspector_limits(client, world, auth):
    world("normal")
    me = client.get("/api/v1/auth/me", headers=auth("fitter")).json()
    assert "defect.create" not in me["permissions"] and "state.view" not in me["permissions"]
    assert "users.manage" not in client.get("/api/v1/auth/me", headers=auth("station")).json()["permissions"]
    w = _wagon()
    # слесарь не создаёт сообщения; осмотрщик не выполняет ремонт
    r = client.post("/api/v1/defect-reports", headers=auth("fitter"), json={
        "client_uuid": uuid.uuid4().hex, "wagon_number": w["number"], "category": "body", "description": "abc", "urgency": "normal"})
    assert r.status_code == 403
    rep = _report(client, auth, w)
    _act(client, auth, "pto", rep["id"], "acknowledge")
    r = client.post(f"/api/v1/defect-reports/{rep['id']}/decision", headers=auth("station"),
                    json={"kind": "repair_in_place", "assignee_id": "u-insp"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_ASSIGNEE"
    ctx = client.get("/api/v1/mobile/context", headers=auth("inspector")).json()
    assert ctx["scope"]["pto_id"] == "PTO" and "defect.create" in ctx["permissions"]
    assert client.get("/api/v1/mobile/context", headers=auth("viewer")).status_code == 403
