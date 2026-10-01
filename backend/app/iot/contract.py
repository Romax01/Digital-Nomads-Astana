"""Версионируемый контракт телеметрии и проверка сообщений.

Каждая проверка возвращает код причины; ошибочные сообщения не применяются к состоянию
станции и записываются в журнал отклонённых (telemetry_rejects).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

SUPPORTED_SCHEMAS = {"1.0"}

EVENT_TYPES = {
    "occupancy": "Занятость участка пути",
    "rfid_read": "Идентификация вагонов (RFID)",
    "position": "Координаты и скорость локомотива",
    "switch_position": "Наблюдаемое положение стрелки",
    "equipment_state": "Состояние погрузочного оборудования",
    "diagnostics": "Диагностика ремонтной зоны",
    "heartbeat": "Heartbeat и техническое состояние",
}

# допустимые единицы и преобразования к базовой
UNITS = {
    "speed": {"base": "km/h", "conv": {"km/h": 1.0, "m/s": 3.6}, "range": (0, 120)},
    "x": {"base": "m", "conv": {"m": 1.0}, "range": (-1000, 3000)},
    "y": {"base": "m", "conv": {"m": 1.0}, "range": (-500, 1500)},
    "load": {"base": "t", "conv": {"t": 1.0, "kg": 0.001}, "range": (0, 80)},
    "axle_temp": {"base": "°C", "conv": {"°C": 1.0}, "range": (-50, 200)},
    "battery": {"base": "%", "conv": {"%": 1.0}, "range": (0, 100)},
    "rssi": {"base": "dBm", "conv": {"dBm": 1.0}, "range": (-130, 0)},
    "uptime": {"base": "s", "conv": {"s": 1.0}, "range": (0, 10 ** 9)},
    "flange": {"base": "mm", "conv": {"mm": 1.0}, "range": (0, 60)},
}

REQUIRED = ["schema_version", "event_id", "device_id", "station_id", "object_id", "event_type",
            "observed_at", "sequence_number", "boot_id", "payload", "quality", "source_mode"]

MAX_FUTURE_SKEW = timedelta(seconds=5)
MAX_AGE_ACCEPT = timedelta(hours=24)


class Reject(Exception):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass
class Message:
    schema_version: str
    event_id: str
    device_id: str
    station_id: str
    object_id: str
    event_type: str
    observed_at: datetime
    sequence_number: int
    boot_id: str
    payload: dict
    quality: str
    source_mode: str


def parse_time(v) -> datetime:
    if not isinstance(v, str):
        raise Reject("BAD_TIME", "observed_at должно быть строкой ISO 8601")
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise Reject("BAD_TIME", f"Некорректное время observed_at: {v!r}")
    if dt.tzinfo is None:
        raise Reject("BAD_TIME", "observed_at без часового пояса — время неоднозначно")
    return dt


def validate_structure(d: dict) -> Message:
    if not isinstance(d, dict):
        raise Reject("BAD_STRUCTURE", "Сообщение не является JSON-объектом")
    missing = [k for k in REQUIRED if k not in d]
    if missing:
        raise Reject("BAD_STRUCTURE", f"Отсутствуют обязательные поля: {', '.join(missing)}")
    if str(d["schema_version"]) not in SUPPORTED_SCHEMAS:
        raise Reject("UNKNOWN_SCHEMA", f"Неизвестная версия схемы {d['schema_version']!r}; поддерживаются: "
                                       f"{', '.join(sorted(SUPPORTED_SCHEMAS))}")
    if d["event_type"] not in EVENT_TYPES:
        raise Reject("UNKNOWN_EVENT_TYPE", f"Неизвестный тип события {d['event_type']!r}")
    if not isinstance(d["sequence_number"], int) or d["sequence_number"] < 0:
        raise Reject("BAD_STRUCTURE", "sequence_number должен быть неотрицательным целым")
    if not isinstance(d["payload"], dict):
        raise Reject("BAD_STRUCTURE", "payload должен быть объектом")
    if d["quality"] not in ("good", "uncertain", "bad"):
        raise Reject("BAD_STRUCTURE", "quality: допустимы good, uncertain, bad")
    if d["source_mode"] not in ("simulator", "real"):
        raise Reject("BAD_STRUCTURE", "source_mode: допустимы simulator, real")
    for k in ("event_id", "device_id", "station_id", "object_id", "boot_id"):
        if not isinstance(d[k], str) or not d[k] or len(d[k]) > 64:
            raise Reject("BAD_STRUCTURE", f"Поле {k} должно быть непустой строкой до 64 символов")
    return Message(str(d["schema_version"]), d["event_id"], d["device_id"], d["station_id"], d["object_id"],
                   d["event_type"], parse_time(d["observed_at"]), d["sequence_number"], d["boot_id"],
                   d["payload"], d["quality"], d["source_mode"])


def normalize_measure(name: str, v) -> float:
    spec = UNITS[name]
    if not isinstance(v, dict) or "value" not in v or "unit" not in v:
        raise Reject("BAD_PAYLOAD", f"Величина {name} должна иметь value и unit")
    if v["unit"] not in spec["conv"]:
        raise Reject("BAD_UNIT", f"Недопустимая единица {v['unit']!r} для {name}; допустимы: "
                                 f"{', '.join(spec['conv'])}")
    if not isinstance(v["value"], (int, float)) or isinstance(v["value"], bool):
        raise Reject("BAD_PAYLOAD", f"Значение {name} должно быть числом")
    val = v["value"] * spec["conv"][v["unit"]]
    lo, hi = spec["range"]
    if not lo <= val <= hi:
        raise Reject("OUT_OF_RANGE", f"{name} = {val:g} {spec['base']} вне допустимого диапазона [{lo}; {hi}]")
    return round(val, 3)


def normalize_payload(event_type: str, p: dict) -> dict:
    """Приводит payload к базовым единицам. Возвращает нормализованные значения."""
    if event_type == "occupancy":
        if not isinstance(p.get("occupied"), bool):
            raise Reject("BAD_PAYLOAD", "occupancy: поле occupied должно быть true/false")
        return {"occupied": p["occupied"]}
    if event_type == "switch_position":
        if p.get("position") not in ("normal", "reverse", "unknown"):
            raise Reject("BAD_PAYLOAD", "switch_position: position = normal | reverse | unknown")
        return {"position": p["position"]}
    if event_type == "rfid_read":
        nums = p.get("wagon_numbers")
        if not isinstance(nums, list) or not all(isinstance(n, str) for n in nums):
            raise Reject("BAD_PAYLOAD", "rfid_read: wagon_numbers должен быть списком строк")
        return {"wagon_numbers": nums, "direction": p.get("direction", "in")}
    if event_type == "position":
        out = {"x": normalize_measure("x", p.get("x")), "y": normalize_measure("y", p.get("y"))}
        out["speed"] = normalize_measure("speed", p.get("speed"))
        return out
    if event_type == "equipment_state":
        if p.get("state") not in ("working", "idle", "fault"):
            raise Reject("BAD_PAYLOAD", "equipment_state: state = working | idle | fault")
        out = {"state": p["state"]}
        if "load" in p:
            out["load"] = normalize_measure("load", p["load"])
        return out
    if event_type == "diagnostics":
        out = {}
        for k in ("axle_temp", "flange"):
            if k in p:
                out[k] = normalize_measure(k, p[k])
        if "wagon_number" in p:
            out["wagon_number"] = str(p["wagon_number"])
        if "verdict" in p:
            if p["verdict"] not in ("ok", "faulty"):
                raise Reject("BAD_PAYLOAD", "diagnostics: verdict = ok | faulty")
            out["verdict"] = p["verdict"]
        return out
    if event_type == "heartbeat":
        out = {}
        for k in ("battery", "rssi", "uptime"):
            if k in p:
                out[k] = normalize_measure(k, p[k])
        return out
    raise Reject("UNKNOWN_EVENT_TYPE", event_type)
