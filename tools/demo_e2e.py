"""Сквозная проверка сценария docs/demo.md на запущенном стенде (через API, как интерфейс).

Запуск: docker compose run --rm --no-deps -v ./tools:/tools simulator python /tools/demo_e2e.py
Каждый шаг печатает ✓/✗; код возврата 1 при любой ошибке.
"""
import os
import sys
import time
import uuid

import httpx

API = os.environ.get("BACKEND_URL", "http://backend:8000")
c = httpx.Client(timeout=60)
FAIL = []


def ok(cond, msg):
    print(("✓ " if cond else "✗ ") + msg, flush=True)
    if not cond:
        FAIL.append(msg)
    return cond


def login(u):
    r = c.post(f"{API}/api/v1/auth/login", json={"username": u, "password": "demo123"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


H = {u: login(u) for u in ("admin", "duty", "station", "train", "viewer")}


def post(user, path, body=None, key=True):
    h = dict(H[user])
    if key:
        h["Idempotency-Key"] = uuid.uuid4().hex
    return c.post(f"{API}{path}", headers=h, json=body if body is not None else {})


def get(user, path):
    r = c.get(f"{API}{path}", headers=H[user])
    r.raise_for_status()
    return r.json()


def reset(sc, speed=None, start=False):
    r = c.post(f"{API}/api/v1/sim/reset", headers=H["admin"], json={"scenario": sc, "seed": 42}, timeout=180)
    r.raise_for_status()
    if speed:
        post("admin", "/api/v1/sim/speed", {"speed": speed}, key=False)
    if start:
        post("admin", "/api/v1/sim/start", key=False)
    time.sleep(4)  # симулятор присылает свежую телеметрию


def state():
    for _ in range(20):
        r = c.get(f"{API}/api/v1/state", headers=H["viewer"])
        if r.status_code == 200:
            return r.json()["state"]
        time.sleep(0.5)
    raise RuntimeError("состояние не готово")


def req_id():
    return get("duty", "/api/v1/requests")[0]["id"]


def wait(cond, timeout, step=1.0):
    t = time.time()
    while time.time() - t < timeout:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


print("== 1. Обзор станции")
reset("normal")
st = state()
ok(st["meta"]["is_demo"] and "демо" in st["meta"]["station_name"], "станция помечена как демонстрационная")
ok(len(st["tracks"]) == 14 and all(t["status_label"] for t in st["tracks"].values()), "14 путей со статусом и подписью")
ok(any(t["pos"] for t in st["trains"].values()), "позиции составов рассчитаны backend")
ok(st["index"] and st["index"]["value"] is not None, f"индекс эффективности {st['index'] and st['index']['value']} ({st['index'] and st['index']['quality']['label']})")
ok(all(t["data_state"] == "actual" for t in st["tracks"].values()), "данные всех рельсовых цепей актуальны")
topo = get("viewer", "/api/v1/topology")
ok(set(topo_t["id"] for topo_t in topo["tracks"]) == set(st["tracks"]), "2D/3D-топология и состояние используют одни идентификаторы")

print("== 2а. План выполнен, место есть")
reset("demo_plan_exceeded")
rid = req_id()
r = post("train", f"/api/v1/requests/{rid}/check", key=False).json()["check"]
items = {i["code"]: i for i in r["items"]}
ok(r["decision"] == "available_with_warnings" and items["MONTHLY_PLAN"]["status"] == "warning", r["summary"][:110])
c.put(f"{API}/api/v1/config/plan-policy", headers=H["admin"], json={"policy": "hard_quota"}).raise_for_status()
r2 = post("train", f"/api/v1/requests/{rid}/check", key=False).json()["check"]
ok(r2["decision"] == "unavailable" and "Жёсткая квота" in {i["code"]: i for i in r2["items"]}["MONTHLY_PLAN"]["message"], "жёсткая квота → отказ")
c.put(f"{API}/api/v1/config/plan-policy", headers=H["admin"], json={"policy": "soft"}).raise_for_status()

print("== 2б. Пути заняты")
reset("demo_tracks_busy")
rid = req_id()
r = post("train", f"/api/v1/requests/{rid}/check", key=False).json()["check"]
ok(r["decision"] == "unavailable" and "Ближайшее допустимое окно" in r["summary"], r["summary"])
ok(any(t["code"] == "LENGTH" for t in r["tracks"]) and any(t["code"] == "OCCUPIED" for t in r["tracks"]), "причины по путям: длина и занятость")
ok(r["alternatives"] and all(a["verified"] for a in r["alternatives"]), f"проверенные альтернативы: {[a['type'] for a in r['alternatives']]}")
rc = post("duty", f"/api/v1/requests/{rid}/confirm", {"acknowledge_warnings": True})
ok(rc.status_code == 409 and rc.json()["error"]["code"] == "REQUEST_NOT_AVAILABLE", "прямое подтверждение конфликтной заявки → 409")
alt = next(a for a in r["alternatives"] if a["type"] == "postpone")
ra = post("train", f"/api/v1/requests/{rid}/apply-alternative", {"action": alt["action"]})
ok(ra.status_code == 200 and ra.json()["check"]["decision"].startswith("available"), "перенос на окно → приём возможен")

print("== 2в. Сейчас заполнено, к прибытию освободится")
reset("demo_track_frees")
rid = req_id()
r = post("train", f"/api/v1/requests/{rid}/check", key=False).json()["check"]
tw = {i["code"]: i for i in r["items"]}["TIME_WINDOW"]
ok(r["decision"].startswith("available") and r["assignment"]["tracks"][0] == "ALM-T2" and "освободится" in tw["message"], tw["message"][:120])
rc = post("duty", f"/api/v1/requests/{rid}/confirm", {"acknowledge_warnings": True})
ok(rc.status_code == 200, f"подтверждение дежурным: поезд № {rc.json().get('train_number')}")
aud = get("viewer", "/api/v1/audit?limit=20")
ok(any(a["action"] == "request.confirm" and a["entity_id"] == rid for a in aud), "подтверждение записано в аудит")
num = rc.json().get("train_number")
ok(wait(lambda: any(o["train_number"] == num for o in state()["operations"].values()), 10),
   "новые операции видны в общем состоянии (Гант)")

print("== 3. Потеря связи с датчиком")
reset("sensor_loss", speed=10, start=True)
bad = wait(lambda: state()["tracks"]["ALM-T3"]["data_state"] in ("stale", "missing"), 60)
st = state()
ok(bad, f"путь 3: {st['tracks']['ALM-T3']['status_label']} — {st['tracks']['ALM-T3']['data_message']}")
ok(st["tracks"]["ALM-T3"]["status"] == "unknown" and "A-ALM-T3" in st["alerts"], "статус «Неизвестно» и предупреждение на основном экране")
rid = req_id()
r = post("train", f"/api/v1/requests/{rid}/check", key=False).json()["check"]
t3 = next((t for t in r["tracks"] if t["track_id"] == "ALM-T3"), None)
ok(t3 is not None and t3["code"] == "DATA", "проверка заявки исключает путь 3: нет достоверных данных")
rec = wait(lambda: state()["tracks"]["ALM-T3"]["data_state"] == "actual", 90)
ok(rec, "после перезапуска устройства данные снова актуальны")
ev = get("viewer", "/api/v1/events?limit=200")
ok(any(e["type"] == "device.rebooted" for e in ev), "журнал: устройство перезапущено (новый boot_id)")
ok(any(e["type"] == "data.quality" and "восстановлены" in e["message"] for e in ev), "журнал: данные восстановлены и подтверждены")

print("== 4. Инцидент → конфликт → CP-SAT → рекомендация → решение → аудит → история")
reset("iot_e2e", speed=30)
t_start = time.time()
rid = req_id()
post("train", f"/api/v1/requests/{rid}/check", key=False)
rc = post("duty", f"/api/v1/requests/{rid}/confirm", {"acknowledge_warnings": True})
ok(rc.status_code == 200, "заявка Z-0001 подтверждена")
post("admin", "/api/v1/sim/start", key=False)
inc = wait(lambda: [i for i in get("viewer", "/api/v1/incidents") if i["kind"] == "track_closure"], 120)
ok(inc, f"инцидент на запланированном пути: {inc and inc[0]['title']}")
conf = wait(lambda: [c_ for c_ in state()["conflicts"].values() if c_["type"] in ("track_closed", "track_overlap")], 30)
ok(conf, f"конфликт обнаружен: {conf and conf[0]['explanation'][:100]}")
post("admin", "/api/v1/sim/pause", key=False)
recs = wait(lambda: [x for x in get("station", "/api/v1/recommendations") if x["status"] == "active"], 40)
ok(recs, f"рекомендация планировщика: {recs and recs[0]['title']}")
if recs:
    e = recs[0]["effect"]
    ok(e.get("solver_status") and e.get("solve_ms") is not None, f"эффект: конфликты {e.get('conflicts_before')}→{e.get('conflicts_after')}, "
       f"задержка {e.get('delay_before_min')}→{e.get('delay_after_min')} мин, индекс {e.get('index_before')}→{e.get('index_after')}, {e.get('solver_status')}")
    rr = post("duty", f"/api/v1/recommendations/{recs[0]['id']}/apply")
    ok(rr.status_code == 403, "дежурный не применяет план (право станционного диспетчера)")
    rr = post("station", f"/api/v1/recommendations/{recs[0]['id']}/apply")
    ok(rr.status_code == 200, f"план применён станционным диспетчером: {rr.json().get('changed_operations')} операций"
       if rr.status_code == 200 else f"применение: {rr.status_code} {rr.text[:150]}")
time.sleep(2)
st = state()
ok(not [x for x in st["conflicts"].values() if x["type"] == "track_closed" and x["severity"] == "critical"], "критичных конфликтов закрытия после применения нет")
aud = get("viewer", "/api/v1/audit?limit=50")
ok(any(a["action"] == "plan.apply" for a in aud), "применение плана в аудите")
from datetime import datetime, timedelta, timezone  # noqa: E402
to = datetime.now(timezone.utc)
fr = to - timedelta(seconds=min(600, time.time() - t_start + 5))
rep = c.get(f"{API}/api/v1/replay/window", headers=H["viewer"], params={"from": fr.isoformat(), "to": to.isoformat()}).json()
ok(rep["snapshot"]["state"] and len(rep["deltas"]) > 10, f"история: снимок + {len(rep['deltas'])} дельт")
for kind in ("pdf", "csv"):
    r = c.get(f"{API}/api/v1/reports/mini", headers=H["viewer"], params={"format": kind, "minutes": 15})
    ok(r.status_code == 200 and len(r.content) > 500, f"отчёт {kind.upper()} ({len(r.content)} байт)")

print("== 5. 10 одновременных нештатных ситуаций")
reset("multi_10", speed=60, start=True)
wait(lambda: len([i for i in get("viewer", "/api/v1/incidents") if i["status"] == "active"]) >= 9, 40)
post("admin", "/api/v1/sim/pause", key=False)
t = time.time()
r = post("station", "/api/v1/plans/compute", key=False)
dt = time.time() - t
ok(r.status_code == 200 and dt <= 5.0, f"пересчёт за {dt:.2f} с: {r.json().get('solver_status_label')}")

print("== 6. Помощник и права")
for q in ("Почему нельзя принять этот состав?", "Когда появится ближайшее окно?", "Что произойдёт, если путь 3 закроется на час?",
          "Какие операции создают перегрузку?", "Как уменьшить задержку?"):
    a = c.post(f"{API}/api/v1/assistant/ask", headers=H["viewer"], json={"question": q}).json()
    ok(a.get("text") and a.get("computed_at"), f"«{q}» → {a.get('text', '')[:90]}")
ok(post("viewer", "/api/v1/sim/pause", key=False).status_code == 403, "наблюдатель не управляет симуляцией")

reset("normal", speed=5, start=True)
print(f"\nИтого ошибок: {len(FAIL)}")
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
