"""Нагрузочный стенд и замеры производительности.

Запуск (внутри сети docker compose, образ симулятора содержит httpx и paho-mqtt):
    docker compose run --rm -v ./tools:/tools simulator python /tools/bench.py

Что измеряется:
  1. Время ответа API (p50/p95/max): состояние, расписание, проверка заявки.
  2. Пересчёт плана после сбоя: сценарии multi_5 и multi_10 (CP-SAT, лимит 4 с).
  3. Поток повышенной интенсивности: N сообщений/с телеметрии сверх фона симулятора;
     задержки этапов (приём, сохранение, обновление модели, отправка в браузер) из /api/v1/metrics.
Результат печатается в Markdown (для docs/performance.md).
"""
import json
import os
import statistics
import time
import uuid
from datetime import datetime, timezone

import asyncio
import threading

import httpx
import paho.mqtt.client as mqtt

API = os.environ.get("BACKEND_URL", "http://backend:8000")
RATE = int(os.environ.get("BENCH_RATE", "300"))
SECONDS = int(os.environ.get("BENCH_SECONDS", "15"))


def pct(v, p):
    v = sorted(v)
    if not v:
        return None
    k = (len(v) - 1) * p
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return round(v[f] + (v[c] - v[f]) * (k - f), 1)


def stats(v):
    return f"{pct(v, .5)} / {pct(v, .95)} / {round(max(v), 1)} (n={len(v)})"


def login(c, u):
    r = c.post(f"{API}/api/v1/auth/login", json={"username": u, "password": "demo123"})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def timed(c, method, url, h, n, **kw):
    out = []
    for _ in range(n):
        t = time.perf_counter()
        r = getattr(c, method)(url, headers=h, **kw)
        out.append((time.perf_counter() - t) * 1000)
        r.raise_for_status()
    return out


def reset(c, adm, scenario):
    c.post(f"{API}/api/v1/sim/reset", headers=adm, json={"scenario": scenario, "seed": 42}).raise_for_status()
    time.sleep(4)  # телеметрия симулятора восстанавливает актуальность данных


WS_SAMPLES: list = []


def ws_listener(token: str, stop: threading.Event):
    """WebSocket-клиент в той же Docker-VM (общие часы с backend): задержка «событие в backend →
    дельта получена клиентом». Отрисовка браузером сюда не входит и измеряется отдельно."""
    import websockets

    async def run():
        async with websockets.connect(API.replace("http", "ws") + f"/ws?token={token}", max_size=None) as ws:
            await ws.send(json.dumps({"type": "hello", "last_version": -1}))
            while not stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), 1)
                except asyncio.TimeoutError:
                    continue
                m = json.loads(raw)
                if m.get("type") == "delta" and (m.get("trace") or {}).get("event_received_at"):
                    WS_SAMPLES.append((time.time() - m["trace"]["event_received_at"]) * 1000)
    asyncio.run(run())


def main():
    c = httpx.Client(timeout=60)
    adm, duty, st = login(c, "admin"), login(c, "duty"), login(c, "station")
    report = ["## Результаты замеров", "", f"Время: {datetime.now(timezone.utc).isoformat()}", ""]

    reset(c, adm, "normal")
    rid = c.get(f"{API}/api/v1/requests", headers=duty).json()[0]["id"]
    report += ["### Время ответа API, мс (p50 / p95 / max)", "",
               "| Запрос | p50 / p95 / max |", "|---|---|",
               f"| GET /state (полное состояние) | {stats(timed(c, 'get', f'{API}/api/v1/state', duty, 30))} |",
               f"| GET /schedule | {stats(timed(c, 'get', f'{API}/api/v1/schedule', duty, 30))} |",
               f"| POST /requests/{{id}}/check (проверка приёма) | {stats(timed(c, 'post', f'{API}/api/v1/requests/{rid}/check', duty, 30))} |",
               ""]

    report += ["### Пересчёт плана после сбоя (CP-SAT, лимит 4 с)", "",
               "| Сценарий | Инцидентов | Статус решения | Расчёт CP-SAT, мс | Полное время API, мс | Изменено операций |",
               "|---|---|---|---|---|---|"]
    for sc in ("track_closure", "multi_5", "multi_10"):
        reset(c, adm, sc)
        c.post(f"{API}/api/v1/sim/speed", headers=adm, json={"speed": 60}).raise_for_status()
        c.post(f"{API}/api/v1/sim/start", headers=adm).raise_for_status()
        deadline = time.time() + 30
        while time.time() < deadline:
            inc = [i for i in c.get(f"{API}/api/v1/incidents", headers=st).json() if i["status"] == "active"]
            if (sc == "track_closure" and inc) or (sc != "track_closure" and len(inc) >= (4 if sc == "multi_5" else 9)):
                break
            time.sleep(1)
        c.post(f"{API}/api/v1/sim/pause", headers=adm)
        time.sleep(2)
        t = time.perf_counter()
        r = c.post(f"{API}/api/v1/plans/compute", headers=st)
        total = (time.perf_counter() - t) * 1000
        p = r.json()
        report.append(f"| {sc} | {len(inc)} | {p.get('solver_status_label')} | {round(p.get('solve_ms', 0))} | {round(total)} | {len(p.get('changes', []))} |")
    report.append("")

    # поток повышенной интенсивности
    reset(c, adm, "normal")
    c.post(f"{API}/api/v1/sim/start", headers=adm)
    time.sleep(5)
    devices = [d for d in c.get(f"{API}/api/v1/devices", headers=st).json()["devices"]]
    cli = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"bench-{uuid.uuid4().hex[:6]}")
    cli.username_pw_set(os.environ.get("MQTT_USER", "simulator"), os.environ.get("MQTT_PASSWORD", ""))
    cli.connect(os.environ.get("MQTT_HOST", "mqtt"), 1883)
    cli.loop_start()
    stop = threading.Event()
    th = threading.Thread(target=ws_listener, args=(st["Authorization"][7:], stop), daemon=True)
    th.start()
    time.sleep(2)
    seq = 10 ** 7
    sent = 0
    t_end = time.time() + SECONDS
    while time.time() < t_end:
        t0 = time.time()
        for i in range(RATE):
            d = devices[i % len(devices)]
            seq += 1
            msg = {"schema_version": "1.0", "event_id": uuid.uuid4().hex, "device_id": d["id"], "station_id": "ALM",
                   "object_id": d["object_id"], "event_type": "heartbeat",
                   "observed_at": datetime.now(timezone.utc).isoformat(), "sequence_number": seq,
                   "boot_id": "bench-boot", "payload": {"battery": {"value": 90, "unit": "%"}},
                   "quality": "good", "source_mode": "simulator"}
            cli.publish(f"station/ALM/{d['kind']}/{d['id']}", json.dumps(msg), qos=1)
            sent += 1
        time.sleep(max(0, 1 - (time.time() - t0)))
    time.sleep(3)
    stop.set()
    cli.loop_stop()
    m = c.get(f"{API}/api/v1/metrics", headers=st).json()
    report += [f"### Поток повышенной интенсивности: {RATE} сообщений/с × {SECONDS} с (отправлено {sent}) поверх фона симулятора", "",
               "| Этап | n | p50, мс | p95, мс | max, мс |", "|---|---|---|---|---|"]
    for k in ("telemetry_transport_ms", "telemetry_persist_ms", "model_update_ms", "state_build_ms", "event_to_ws_ms",
              "client_render_ms", "event_to_screen_ms", "plan_compute_ms", "check_ms", "api_ms", "ws_rtt_ms"):
        s = m["stages"].get(k)
        if s:
            report.append(f"| {s['label']} | {s['count']} | {s['p50']} | {s['p95']} | {s['max']} |")
    if WS_SAMPLES:
        report.append(f"| Событие в backend → дельта получена WebSocket-клиентом | {len(WS_SAMPLES)} | {pct(WS_SAMPLES, .5)} | "
                      f"{pct(WS_SAMPLES, .95)} | {round(max(WS_SAMPLES), 1)} |")
    report += ["", f"Счётчики: `{json.dumps(m['counters'], ensure_ascii=False)}`",
               f"Очередь приёма после теста: `{json.dumps(m['ingest']['queue'], ensure_ascii=False)}`", ""]
    reset(c, adm, "normal")
    c.post(f"{API}/api/v1/sim/start", headers=adm)
    print("\n".join(report))


if __name__ == "__main__":
    main()
