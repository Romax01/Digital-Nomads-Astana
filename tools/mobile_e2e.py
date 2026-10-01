"""Сквозная проверка мобильного процесса работников через сеть (как телефон в Wi-Fi):
осмотрщик → фото и сообщение → оператор ПТО → решение диспетчера → слесарь → приёмка
старшим осмотрщиком; уточнение и отказ; замена (до операции по составу); изоляция данных.

Запуск: BACKEND_URL=http://<IP-компьютера>:8080 python tools/mobile_e2e.py
Не сбрасывает мир — работает поверх текущего состояния демо-станции."""
import io
import json
import os
import sys
import urllib.request
import uuid

B = os.environ.get("BACKEND_URL", "http://localhost:8080").rstrip("/")
errors = 0


def call(method, path, body=None, token=None, raw=None, ctype=None, key=None):
    h = {}
    if token:
        h["Authorization"] = "Bearer " + token
    data = None
    if raw is not None:
        data, h["Content-Type"] = raw, ctype
    elif body is not None:
        data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    if key:
        h["Idempotency-Key"] = key
    req = urllib.request.Request(B + path, method=method, headers=h, data=data)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def ok(cond, text):
    global errors
    print(("✓ " if cond else "✗ ") + text)
    if not cond:
        errors += 1


def login(u):
    s, r = call("POST", "/api/v1/auth/login", {"username": u, "password": "demo123"})
    assert s == 200, (u, r)
    return r["token"]


def jpeg() -> bytes:
    try:
        from PIL import Image
        b = io.BytesIO()
        Image.new("RGB", (320, 240), (120, 60, 40)).save(b, "JPEG")
        return b.getvalue()
    except ImportError:
        return b"\xff\xd8\xff\xe0" + b"\x00" * 400 + b"\xff\xd9"


T = {u: login(u) for u in ("inspector", "pto", "station", "fitter", "fitter2", "senior", "otr_inspector")}
print(f"== Сервер {B}")
s, ctx = call("GET", "/api/v1/mobile/context", token=T["inspector"])
ok(s == 200 and "defect.create" in ctx["permissions"], f"контекст осмотрщика: {ctx['scope']['station_name']}, {ctx['user']['role_label']}")
ok(call("GET", "/api/v1/state", token=T["inspector"])[0] == 403, "рабочей роли полный снимок станции не выдаётся (403)")

print("== 1. Сообщение с фото → приём → решение → работы → приёмка")
s, trains = call("GET", "/api/v1/mobile/trains", token=T["inspector"])
wagons = [(t, w) for t in trains for w in t["wagons"] if w["condition"] == "ok"]
ok(bool(wagons), f"составов на станции: {len(trains)}, исправных вагонов для примера: {len(wagons)}")
t, w = wagons[0]
s, att = call("POST", f"/api/v1/attachments?client_uuid={uuid.uuid4().hex}&filename=defect.jpg", raw=jpeg(), ctype="image/jpeg", token=T["inspector"])
ok(s == 200, f"фото загружено ({att.get('size')} байт)")
cu = uuid.uuid4().hex
body = {"client_uuid": cu, "wagon_id": w["id"], "wagon_number": w["number"], "train_id": t["id"], "track_id": t["track_id"],
        "position": w["position"], "category": "axlebox", "component": "Букса, 2-я колёсная пара",
        "description": "Нагрев буксы, выброс смазки (проверка mobile_e2e)", "urgency": "urgent", "attachment_ids": [att["id"]]}
s, rep = call("POST", "/api/v1/defect-reports", body, token=T["inspector"])
ok(s == 200 and rep["status"] == "submitted", f"сообщение № {rep.get('number')} доставлено, получение не подтверждено")
s, rep2 = call("POST", "/api/v1/defect-reports", body, token=T["inspector"])
ok(rep2["id"] == rep["id"] and rep2["idempotent_replay"], "повторная отправка офлайн-очереди не создаёт дубль")
s, r = call("POST", f"/api/v1/defect-reports/{rep['id']}/actions/acknowledge", {"expected_version": rep["version"]}, token=T["pto"], key=uuid.uuid4().hex)
ok(s == 200 and r["acknowledged_at"], "оператор ПТО подтвердил получение")
s, r = call("POST", f"/api/v1/defect-reports/{rep['id']}/decision", {"kind": "repair_in_place", "assignee_id": "u-fitter",
            "reason": "Ремонт на месте допустим (e2e)"}, token=T["station"], key=uuid.uuid4().hex)
ok(s == 200, f"диспетчер принял решение: {r.get('work_order', {}).get('kind_label')}")
wo = r["work_order"]
ok(call("GET", f"/api/v1/work-orders/{wo['id']}", token=T["fitter2"])[0] == 404, "задание не видно исполнителю другой бригады (BR-2)")
s, r = call("POST", f"/api/v1/work-orders/{wo['id']}/actions/start", {}, token=T["fitter"], key=uuid.uuid4().hex)
ok(s == 200 and r["status"] == "in_progress", "слесарь начал работы")
s, a2 = call("POST", f"/api/v1/attachments?client_uuid={uuid.uuid4().hex}&filename=result.jpg", raw=jpeg(), ctype="image/jpeg", token=T["fitter"])
s, r = call("POST", f"/api/v1/work-orders/{wo['id']}/actions/submit", {"report": "Букса перебрана, смазка заменена", "attachment_ids": [a2["id"]]},
            token=T["fitter"], key=uuid.uuid4().hex)
ok(s == 200 and r["status"] == "awaiting_inspection", "результат передан на контрольный осмотр")
s, r = call("POST", f"/api/v1/work-orders/{wo['id']}/actions/accept", {"comment": "Проверено (e2e)"}, token=T["senior"], key=uuid.uuid4().hex)
ok(s == 200 and r["status"] == "completed" and r["wagon"]["condition"] == "ok", f"старший осмотрщик принял: вагон «{r['wagon']['condition_label']}»")
s, card = call("GET", f"/api/v1/defect-reports/{rep['id']}", token=T["inspector"])
ok(len(card["attachments"]) == 1 and len(card["timeline"]) >= 4, f"автор видит хронологию ({len(card['timeline'])} записей) и фото")
s, n = call("GET", "/api/v1/notifications?unread=true", token=T["inspector"])
ok(any(x["kind"] == "work_order.completed" for x in n["items"]), "автору пришло уведомление о завершении")

print("== 2. Уточнение и отказ с основанием")
t2, w2 = wagons[1]
s, d = call("POST", "/api/v1/defect-reports", {**body, "client_uuid": uuid.uuid4().hex, "wagon_id": w2["id"], "wagon_number": w2["number"],
            "urgency": "normal", "attachment_ids": []}, token=T["inspector"])
s, r = call("POST", f"/api/v1/defect-reports/{d['id']}/actions/needs_info", {"reason": "Уточните сторону"}, token=T["pto"], key=uuid.uuid4().hex)
ok(r["status"] == "needs_info", "запрошено уточнение")
s, r = call("POST", f"/api/v1/defect-reports/{d['id']}/actions/reply", {"reason": "Правая сторона"}, token=T["inspector"], key=uuid.uuid4().hex)
ok(r["status"] == "under_review", "автор ответил — сообщение снова на рассмотрении")
s, r = call("POST", f"/api/v1/defect-reports/{d['id']}/actions/reject", {}, token=T["pto"], key=uuid.uuid4().hex)
ok(s == 400, "отказ без основания отклонён")
s, r = call("POST", f"/api/v1/defect-reports/{d['id']}/actions/reject", {"reason": "Не подтверждено повторным осмотром (e2e)"}, token=T["pto"], key=uuid.uuid4().hex)
ok(r["status"] == "rejected", "отказ с основанием записан")

print("== 3. Изоляция станций и вложений")
ok(call("GET", f"/api/v1/defect-reports/{rep['id']}", token=T["otr_inspector"])[0] == 404, "работник другой станции не видит сообщение")
ok(call("GET", card["attachments"][0]["url"], token=T["otr_inspector"])[0] == 404, "и не скачивает фото")
ok(call("GET", "/api/v1/admin/users", token=T["senior"])[0] == 403, "административный API закрыт для рабочих ролей")

print(f"\nИтого ошибок: {errors}")
sys.exit(1 if errors else 0)
