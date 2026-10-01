"""Симулятор IoT-устройств станции (source_mode = simulator).

Читает «эталонный мир» симуляции у backend и публикует показания устройств в MQTT по
версионируемому контракту телеметрии 1.0. Моделирует:
  * периодические измерения и отправку при изменении;
  * heartbeat и техническое состояние (батарея, уровень сигнала, время работы);
  * последовательность сообщений (sequence_number) в рамках сеанса (boot_id);
  * ограниченный буфер отправителя при потере связи с брокером и досылку после восстановления;
  * неисправности по сценарию: молчание, перезапуск, «залипание», дубли, нарушение порядка,
    неверная единица измерения.
Учётные данные MQTT берутся из переменных окружения; на frontend они не передаются.
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

import httpx
import paho.mqtt.client as mqtt

log = logging.getLogger("simulator")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}')

BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000")
TOKEN = os.environ.get("SIM_WORLD_TOKEN", "sim_world_dev_token")
SEED = int(os.environ.get("SIM_SEED", "42"))
LOAD_FACTOR = float(os.environ.get("SIM_LOAD_FACTOR", "1"))
HB_PERIOD = {"track_circuit": 5, "switch_sensor": 10, "rfid_reader": 5, "loco_gps": 10, "cargo_equipment": 10,
             "repair_diag": 5}
RETAIN = {"occupancy", "switch_position"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Device:
    def __init__(self, d: dict, rnd: random.Random):
        self.id, self.kind, self.object_id, self.period = d["id"], d["kind"], d["object_id"], float(d["period_s"])
        self.boot_id = f"boot-{uuid.UUID(int=rnd.getrandbits(128)).hex[:8]}"
        self.seq = 0
        self.last_value = None
        self.next_meas = 0.0
        self.next_hb = rnd.uniform(0, 2)
        self.started = time.time()
        self.battery = rnd.uniform(70, 100)
        self.rssi = rnd.uniform(-85, -55)
        self.seen_ops: set[str] = set()


class Simulator:
    def __init__(self):
        self.rnd = random.Random(SEED)
        self.devices: dict[str, Device] = {}
        self.station = None
        self.buffer: deque = deque(maxlen=2000)   # ограниченный буфер отправителя
        self.connected = False
        self.held: list = []                      # для режима out_of_order
        self.faulty_sent: set = set()
        self.rebooted: set = set()
        self.lock = threading.Lock()
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="station-simulator")
        self.client.username_pw_set(os.environ.get("MQTT_USER", "simulator"), os.environ.get("MQTT_PASSWORD", ""))
        self.client.reconnect_delay_set(1, 10)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.sent = 0
        self.dropped = 0

    def on_connect(self, c, u, f, rc, p=None):
        self.connected = not rc.is_failure
        if self.connected:
            log.info("MQTT подключено; досылка буфера: %d сообщений", len(self.buffer))
            while self.buffer:
                topic, payload, retain = self.buffer.popleft()
                self.client.publish(topic, payload, qos=1, retain=retain)

    def on_disconnect(self, c, u, f, rc, p=None):
        self.connected = False
        log.warning("MQTT соединение потеряно (%s); сообщения копятся в буфере (до %d)", rc, self.buffer.maxlen)

    # ------------------------------------------------------------- публикация
    def publish(self, dev: Device, event_type: str, payload: dict, fault: dict | None = None):
        dev.seq += 1
        msg = {"schema_version": "1.0", "event_id": uuid.uuid4().hex, "device_id": dev.id, "station_id": self.station,
               "object_id": dev.object_id, "event_type": event_type, "observed_at": now_iso(),
               "sequence_number": dev.seq, "boot_id": dev.boot_id, "payload": payload, "quality": "good",
               "source_mode": "simulator"}
        mode = (fault or {}).get("mode")
        if mode == "wrong_unit" and event_type == "position":
            msg["payload"] = {**payload, "speed": {"value": payload["speed"]["value"] / 1.609, "unit": "mph"}}
        topic = f"station/{self.station}/{dev.kind}/{dev.id}"
        data = json.dumps(msg, ensure_ascii=False)
        retain = event_type in RETAIN
        if mode == "out_of_order" and self.rnd.random() < 0.5:
            self.held.append((topic, data, retain))
            return
        self._send(topic, data, retain)
        if mode == "duplicate":
            self._send(topic, data, retain)  # повторная доставка того же event_id
        if self.held and self.rnd.random() < 0.5:
            self._send(*self.held.pop(0))

    def _send(self, topic, data, retain):
        if self.connected:
            info = self.client.publish(topic, data, qos=1, retain=retain)
            if info.rc == mqtt.MQTT_ERR_SUCCESS:
                self.sent += 1
                return
        if len(self.buffer) == self.buffer.maxlen:
            self.dropped += 1
        self.buffer.append((topic, data, retain))

    # ------------------------------------------------------------- цикл
    def fetch_world(self) -> dict | None:
        try:
            r = httpx.get(f"{BACKEND}/api/v1/sim/world", headers={"X-Sim-Token": TOKEN}, timeout=3)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("Нет связи с backend: %s", e)
            return None

    def sync_devices(self, w: dict):
        self.station = w["station_id"]
        ids = set()
        for d in w["devices"]:
            ids.add(d["id"])
            if d["id"] not in self.devices:
                self.devices[d["id"]] = Device(d, self.rnd)
        for k in list(self.devices):
            if k not in ids:
                del self.devices[k]

    def step(self, w: dict):
        t = time.time()
        faults = w.get("faults", {})
        flags = w.get("flags", {})
        tracks = {x["id"]: x["occupied"] for x in w["tracks"]}
        switches = {x["id"]: x["position"] for x in w["switches"]}
        locos = {x["resource_id"]: x for x in w["locos"]}
        equip = {x["resource_id"]: x for x in w["equipment"]}
        for dev in self.devices.values():
            f = faults.get(dev.id)
            if f:
                until = datetime.fromisoformat(f["until"]).timestamp()
                if t < until:
                    if f["mode"] == "silent":
                        continue  # устройство молчит: ни измерений, ни heartbeat
                else:
                    key = (dev.id, f["until"])
                    if f.get("then") == "reboot" and key not in self.rebooted:
                        self.rebooted.add(key)
                        dev.boot_id = f"boot-{uuid.uuid4().hex[:8]}"
                        dev.seq = 0
                        dev.started = t
                        dev.last_value = None
                        log.info("Устройство %s перезапущено (новый boot_id %s)", dev.id, dev.boot_id)
                    f = None
            if t >= dev.next_hb:
                dev.next_hb = t + HB_PERIOD.get(dev.kind, 5)
                dev.battery = max(5.0, dev.battery - self.rnd.uniform(0, 0.05))
                self.publish(dev, "heartbeat", {"battery": {"value": round(dev.battery, 1), "unit": "%"},
                                                "rssi": {"value": round(dev.rssi + self.rnd.uniform(-3, 3), 1), "unit": "dBm"},
                                                "uptime": {"value": int(t - dev.started), "unit": "s"}}, f)
            period = dev.period / (LOAD_FACTOR if dev.kind == "loco_gps" else 1)
            if dev.kind == "track_circuit":
                val = tracks.get(dev.object_id)
                if f and f["mode"] == "stuck_free":
                    val = False
                if val is not None and (val != dev.last_value or t >= dev.next_meas):
                    dev.last_value, dev.next_meas = val, t + period
                    self.publish(dev, "occupancy", {"occupied": bool(val)}, f)
            elif dev.kind == "switch_sensor":
                val = switches.get(dev.object_id)
                if val and (val != dev.last_value or t >= dev.next_meas):
                    dev.last_value, dev.next_meas = val, t + period
                    self.publish(dev, "switch_position", {"position": val}, f)
            elif dev.kind == "loco_gps":
                lo = locos.get(dev.object_id)
                if lo and t >= dev.next_meas:
                    dev.next_meas = t + period
                    jitter = lambda: self.rnd.gauss(0, 0.8)  # noqa: E731  погрешность ГНСС, м
                    self.publish(dev, "position", {"x": {"value": round(lo["x"] + jitter(), 2), "unit": "m"},
                                                   "y": {"value": round(lo["y"] + jitter(), 2), "unit": "m"},
                                                   "speed": {"value": lo["speed_kmh"], "unit": "km/h"}}, f)
            elif dev.kind == "cargo_equipment":
                eq = equip.get(dev.object_id)
                if eq and (eq["state"] != dev.last_value or t >= dev.next_meas):
                    dev.last_value, dev.next_meas = eq["state"], t + period
                    self.publish(dev, "equipment_state", {"state": eq["state"], "load": {"value": eq["load_t"], "unit": "t"}}, f)
            elif dev.kind == "rfid_reader":
                for pas in w.get("rfid", []):
                    if pas["reader"] == dev.id and pas["op_id"] not in dev.seen_ops:
                        dev.seen_ops.add(pas["op_id"])
                        self.publish(dev, "rfid_read", {"wagon_numbers": pas["wagons"], "direction": pas["direction"]}, f)
            elif dev.kind == "repair_diag":
                for ins in w.get("inspections", []):
                    if ins["op_id"] in dev.seen_ops or not ins["wagons"]:
                        continue
                    dev.seen_ops.add(ins["op_id"])
                    faulty = flags.get("diag_faulty_next") and ins["op_id"] not in self.faulty_sent
                    wagon = ins["wagons"][len(ins["wagons"]) // 2]
                    if faulty:
                        self.faulty_sent.add(ins["op_id"])
                        self.publish(dev, "diagnostics", {"wagon_number": wagon, "axle_temp": {"value": 96.5, "unit": "°C"},
                                                          "flange": {"value": 23.1, "unit": "mm"}, "verdict": "faulty"}, f)
                    else:
                        self.publish(dev, "diagnostics", {"wagon_number": wagon, "axle_temp": {"value": round(self.rnd.uniform(35, 55), 1), "unit": "°C"},
                                                          "flange": {"value": round(self.rnd.uniform(28, 32), 1), "unit": "mm"}, "verdict": "ok"}, f)

    def run(self):
        host, port = os.environ.get("MQTT_HOST", "localhost"), int(os.environ.get("MQTT_PORT", "1883"))
        self.client.connect_async(host, port, keepalive=15)
        self.client.loop_start()
        last_log = 0
        world = None
        last_fetch = 0.0
        while True:
            t = time.time()
            if t - last_fetch >= 1.0:
                w = self.fetch_world()
                if w:
                    world = w
                    self.sync_devices(w)
                last_fetch = t
            if world:
                with self.lock:
                    self.step(world)
            if t - last_log > 30:
                last_log = t
                log.info("Устройств: %d, отправлено: %d, в буфере: %d, потеряно при переполнении буфера: %d",
                         len(self.devices), self.sent, len(self.buffer), self.dropped)
            time.sleep(0.2)


if __name__ == "__main__":
    Simulator().run()
