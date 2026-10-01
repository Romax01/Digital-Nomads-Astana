"""«Эталонный мир» симуляции для симулятора IoT.

Симулятор датчиков читает отсюда фактическое (модельное) положение поездов, занятость путей,
положение стрелок и состояние оборудования и формирует из него показания устройств с
задержками, шумом и неисправностями. Это внутренний канал стенда (защищён отдельным токеном),
он не используется интерфейсом и не попадает в браузер.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import aware, iso, utcnow
from app.domain.topology import point_at
from app.iot.quality import expected_occupancy
from app.models import Device, Zone
from app.services.model import MOVEMENT_KINDS, StationModel
from app.services.view import train_position


def world(db: Session) -> dict:
    model = StationModel(db, data_states={})
    now = model.now
    occ = expected_occupancy(model)
    devices = list(db.execute(select(Device)).scalars())
    by_obj = {}
    for d in devices:
        by_obj.setdefault(d.object_id, []).append(d)
    route_sw = {}
    for o in model.ops.values():
        if o.status == "in_progress" and o.kind in MOVEMENT_KINDS:
            for sw in o.route_nodes or []:
                route_sw[sw] = o.id
    zones = {z.id: z for z in db.execute(select(Zone)).scalars()}
    tracks = [{"id": tid, "occupied": tid in occ} for tid in model.track_rows]
    switches = [{"id": nid, "position": "reverse" if nid in route_sw else "normal"}
                for nid, n in model.topo.nodes.items() if n["kind"] == "switch"]
    locos, equipment = [], []
    for rid, r in model.resources.items():
        cur = next((o for o in model.ops.values() if o.status == "in_progress" and rid in (o.resource_ids or [])), None)
        if r.kind == "shunting_loco":
            x = y = None
            speed = 0.0
            if cur and cur.train_id and cur.train_id in model.trains:
                t = model.trains[cur.train_id]
                p = train_position(model, t, model.ops_by_train.get(t.id, []))
                if p:
                    x, y, _ = point_at(p["path"], p["head_s"])
                    speed = round(min(40.0, p["speed"] * 3.6 * 60 / 10), 1) if p["moving"] else 0.0
            if x is None:
                z = zones.get(r.home_zone_id)
                x, y = (z.x + 18 * (int(rid.split("-")[-1]) if rid.split("-")[-1].isdigit() else 0), z.y) if z else (0, 0)
            locos.append({"resource_id": rid, "x": round(x, 1), "y": round(y, 1), "speed_kmh": speed})
        if r.kind == "cargo_equipment":
            failed = any(i.kind == "resource_failure" and i.object_id == rid for i in model.incidents)
            equipment.append({"resource_id": rid, "state": "fault" if failed else ("working" if cur else "idle"),
                              "load_t": 18.5 if cur else 0.0})
    inspections, rfid = [], []
    for o in model.ops.values():
        if o.status != "in_progress" or not o.train_id:
            continue
        t = model.trains[o.train_id]
        wl = sorted(model.wagons_by_train.get(t.id, []), key=lambda w: w.position)
        if o.kind == "inspection":
            inspections.append({"op_id": o.id, "train_number": t.number, "wagons": [w.number for w in wl]})
        if o.kind in ("arrival", "departure"):
            side = o.side or (t.arrival_side if o.kind == "arrival" else t.departure_side)
            rfid.append({"reader": f"{model.sid}-RFID-{'W' if side == 'west' else 'E'}", "op_id": o.id,
                         "direction": "in" if o.kind == "arrival" else "out", "wagons": [w.number for w in wl]})
    sim = model.sim
    return {"station_id": model.sid, "model_time": iso(now), "real_time": iso(utcnow()), "speed": sim.speed,
            "running": sim.running, "scenario": sim.scenario, "seed": sim.seed,
            "tracks": tracks, "switches": switches, "locos": locos, "equipment": equipment,
            "inspections": inspections, "rfid": rfid,
            "devices": [{"id": d.id, "kind": d.kind, "object_id": d.object_id, "period_s": d.period_s,
                         "status": d.status} for d in devices],
            "faults": (sim.world or {}).get("device_faults", {}),
            "flags": {k: v for k, v in (sim.world or {}).items() if k != "device_faults"}}
