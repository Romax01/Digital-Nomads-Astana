"""Процесс приёма телеметрии: MQTT-подписка → очередь с приоритетами → пакетная обработка.

* постоянная сессия MQTT (clean_session=False, QoS 1): после перезапуска backend брокер
  доставит накопленные сообщения; повторы отсекаются по event_id;
* значимые изменения (занятость, стрелки, RFID, диагностика) идут вне очереди координат;
* координатные обновления объединяются: для каждого устройства хранится только последнее;
* очередь ограничена: при переполнении новые значимые сообщения отклоняются с записью
  причины, координаты — заменяются (защита от перегрузки).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

from app.config import get_settings
from app.core import bus, metrics
from app.core.timeutil import utcnow
from app.db import SessionLocal
from app.iot.ingest import IngestProcessor, RawItem, classify_priority
from app.models import TelemetryReject

log = logging.getLogger("ingest")


class IngestQueue:
    def __init__(self, maxlen: int):
        self.maxlen = maxlen
        self.significant: deque[RawItem] = deque()
        self.coords: dict[str, RawItem] = {}
        self.cv = threading.Condition()
        self.dropped = 0

    def put(self, item: RawItem):
        prio = classify_priority(item.payload)
        with self.cv:
            if prio == 2:
                key = item.topic
                if key in self.coords:
                    metrics.incr("telemetry_coalesced")
                self.coords[key] = item
            else:
                if len(self.significant) >= self.maxlen:
                    self.dropped += 1
                    metrics.incr("telemetry_queue_overflow")
                    self._overflow(item)
                    return
                self.significant.append(item)
            self.cv.notify()

    def _overflow(self, item: RawItem):
        try:
            with SessionLocal() as db:
                db.add(TelemetryReject(received_at=item.received_at, topic=item.topic[:160], device_id=None,
                                       reason_code="QUEUE_OVERFLOW",
                                       reason="Очередь приёма переполнена — сообщение не обработано (защита от перегрузки)",
                                       raw=item.payload.decode("utf-8", "replace")[:2000]))
                db.commit()
        except Exception:
            pass

    def take(self, max_items: int = 500, wait: float = 0.05) -> list[RawItem]:
        with self.cv:
            if not self.significant and not self.coords:
                self.cv.wait(wait)
            out = []
            while self.significant and len(out) < max_items:
                out.append(self.significant.popleft())
            if len(out) < max_items and self.coords:
                for k in list(self.coords)[: max_items - len(out)]:
                    out.append(self.coords.pop(k))
            return out

    def depth(self) -> dict:
        with self.cv:
            return {"significant": len(self.significant), "coordinates": len(self.coords), "dropped": self.dropped}


class IngestService:
    def __init__(self):
        s = get_settings()
        self.queue = IngestQueue(s.ingest_queue_max)
        self.processor = IngestProcessor()
        self.client = None
        self.connected = False
        self.last_connect_at = None
        self.disconnects = 0
        self.worker: threading.Thread | None = None
        self.stop = False

    def submit(self, topic: str, payload: bytes, retained: bool = False):
        self.queue.put(RawItem(topic=topic, payload=payload, retained=retained))

    def start(self):
        s = get_settings()
        self.worker = threading.Thread(target=self._work, name="ingest-worker", daemon=True)
        self.worker.start()
        if not s.mqtt_enabled:
            return
        import paho.mqtt.client as mqtt
        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id="digital-station-ingest",
                        clean_session=False)
        c.username_pw_set(s.mqtt_user, s.mqtt_password)
        c.reconnect_delay_set(min_delay=1, max_delay=15)
        c.max_inflight_messages_set(100)

        def on_connect(client, userdata, flags, reason_code, properties=None):
            self.connected = not reason_code.is_failure
            if self.connected:
                self.last_connect_at = utcnow()
                client.subscribe(s.mqtt_topic, qos=1)
                log.info("MQTT: подключено, подписка на %s", s.mqtt_topic)
            else:
                log.warning("MQTT: отказ в подключении: %s", reason_code)

        def on_disconnect(client, userdata, flags, reason_code, properties=None):
            self.connected = False
            self.disconnects += 1
            log.warning("MQTT: соединение потеряно (%s), переподключение…", reason_code)

        def on_message(client, userdata, msg):
            self.submit(msg.topic, msg.payload, bool(msg.retain))

        c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, on_message
        self.client = c
        try:
            c.connect_async(s.mqtt_host, s.mqtt_port, keepalive=15)
        except Exception as e:
            log.warning("MQTT: не удалось начать подключение: %s", e)
        c.loop_start()

    def _work(self):
        while not self.stop:
            items = self.queue.take()
            if not items:
                continue
            t0 = time.perf_counter()
            try:
                with SessionLocal() as db:
                    res = self.processor.process_batch(db, items)
                    db.commit()
            except Exception:
                log.exception("Ошибка обработки пакета телеметрии")
                continue
            metrics.observe("telemetry_persist_ms", (time.perf_counter() - t0) * 1000)
            metrics.incr("telemetry_applied", res.applied)
            earliest = min(items, key=lambda i: i.received_mono)
            bus.publish("telemetry_applied", {"received_wall": earliest.received_at.timestamp(),
                                              "received_mono": earliest.received_mono,
                                              "significant": res.significant, "applied": res.applied})

    def status(self) -> dict:
        s = get_settings()
        return {"mqtt_enabled": s.mqtt_enabled, "connected": self.connected,
                "last_connect_at": self.last_connect_at.isoformat() if self.last_connect_at else None,
                "disconnects": self.disconnects, "queue": self.queue.depth(), "broker": f"{s.mqtt_host}:{s.mqtt_port}"}


ingest_service = IngestService()
