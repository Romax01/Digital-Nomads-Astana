"""Сборка единого представления состояния станции для всех экранов (2D, 3D, Гант, панели).

Все представления frontend строятся из этого состояния; 3D-сцена не вычисляет занятость
самостоятельно — позиции составов рассчитаны здесь по модели операций.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import aware, iso, utcnow
from app.domain.statuses import LABELS
from app.iot.quality import STATE_LABELS, device_statuses
from app.models import (
    Incident, Observation, PlanVersion, Recommendation, Resource, TransferRequest, Zone,
)
from app.services.conflicts import detect
from app.services.model import KIND_LABEL, MOVEMENT_KINDS, RES_KIND_LABEL, StationModel
from app.sim.scenarios import SCENARIOS

TRACK_STATUS_LABEL = {"free": "Свободен", "occupied": "Занят", "unknown": "Неизвестно",
                      "contradictory": "Противоречие", "closed": "Закрыт", "reserved": "Зарезервирован"}


def train_position(model: StationModel, train, ops) -> dict | None:
    topo = model.topo
    now = model.now
    L = model.train_length(train)
    for o in ops:
        if o.status == "in_progress" and o.kind in ("arrival", "departure", "shunting"):
            if o.kind == "arrival":
                mp = topo.movement_path("arrival", o.side or train.arrival_side, None, o.track_id, L)
            elif o.kind == "departure":
                mp = topo.movement_path("departure", o.side or train.departure_side, o.track_id, None, L)
            else:
                mp = topo.movement_path("shunting", None, o.from_track_id, o.track_id, L)
            if not mp:
                continue
            pts, s0, s1, body = mp
            total = (o.duration_min + (o.extra_delay_min or 0)) * 60
            p = min(1.0, max(0.0, (now - aware(o.actual_start)).total_seconds() / total)) if total else 1.0
            speed = (s1 - s0) / total if total else 0  # единиц схемы за модельную секунду
            return {"path": [[round(x, 1), round(y, 1)] for x, y in pts], "head_s": round(s0 + (s1 - s0) * p, 2),
                    "head_end": round(s1, 2), "body": round(body, 1), "moving": True, "speed": round(speed, 4),
                    "op_id": o.id, "kind": o.kind}
    if train.status == "on_station" and train.current_track_id:
        pts, head, body = topo.standing_path(train.current_track_id, L, toward_east=(train.arrival_side == "west"))
        return {"path": [[round(x, 1), round(y, 1)] for x, y in pts], "head_s": round(head, 2), "head_end": round(head, 2),
                "body": round(body, 1), "moving": False, "speed": 0}
    if train.status == "waiting":
        arr = next((o for o in ops if o.kind == "arrival"), None)
        if arr:
            mp = topo.movement_path("arrival", train.arrival_side, None, arr.track_id, L)
            if mp:
                pts, s0, s1, body = mp
                return {"path": [[round(x, 1), round(y, 1)] for x, y in pts], "head_s": 0.0, "head_end": 0.0,
                        "body": round(body, 1), "moving": False, "speed": 0, "waiting": True}
    return None


def build_view(db: Session, model: StationModel, engine_waiting: dict, index: dict | None) -> dict:
    now = model.now
    det = detect(model)
    fc = det["forecast"]
    conflicts = det["conflicts"]
    delays = {d["train_id"]: d["delay_min"] for d in det["delays"]}
    conflict_objs: dict[str, list] = {}
    for c in conflicts:
        for ob in c["objects"]:
            conflict_objs.setdefault(ob["id"], []).append(c["id"])
        for oid in c["operations"]:
            conflict_objs.setdefault(oid, []).append(c["id"])

    # ---- пути
    tracks = {}
    closures = {}
    for inc in model.incidents:
        if inc.kind == "track_closure" and aware(inc.start_at) <= now:
            closures[inc.object_id] = inc
    occupant = {t.current_track_id: t for t in model.trains.values() if t.status == "on_station" and t.current_track_id}
    for o in model.ops.values():
        if o.status == "in_progress" and o.kind in ("arrival", "shunting"):
            tr = model.trains.get(o.train_id)
            if tr:
                occupant.setdefault(o.track_id, tr)
    next_res = {}
    for r in model.reservations:
        if r.resource_key.startswith("track:") and aware(r.end_at) > now:
            tid = r.resource_key[6:]
            cur = next_res.get(tid)
            if cur is None or aware(r.start_at) < aware(cur.start_at):
                if aware(r.start_at) > now or not occupant.get(tid):
                    next_res[tid] = r
    for tid, t in model.track_rows.items():
        ds = model.data_states.get(tid, {"state": "not_monitored", "message": "Датчик не установлен"})
        planned_occ = occupant.get(tid)
        if tid in closures:
            status = "closed"
        elif ds["state"] in ("stale", "missing", "invalid"):
            status = "unknown"
        elif ds["state"] == "contradictory":
            status = "contradictory"
        elif ds.get("observed") == "occupied" or (ds["state"] == "not_monitored" and planned_occ):
            status = "occupied"
        else:
            status = "free"
        nr = next_res.get(tid)
        tracks[tid] = {
            "id": tid, "number": t.number, "label": model.track_label(tid), "kind": t.kind, "park_id": t.park_id,
            "useful_length_m": t.useful_length_m, "status": status, "status_label": TRACK_STATUS_LABEL[status],
            "data_state": ds["state"], "data_state_label": STATE_LABELS.get(ds["state"], ds["state"]),
            "data_message": ds.get("message"), "data_observed_at": ds.get("observed_at"), "device_id": ds.get("device_id"),
            "observed": ds.get("observed"), "occupant_train_id": planned_occ.id if planned_occ else None,
            "occupant_number": planned_occ.number if planned_occ else None,
            "closure": {"incident_id": closures[tid].id, "title": closures[tid].title,
                        "until": iso(closures[tid].end_at)} if tid in closures else None,
            "next_reservation": {"start": iso(nr.start_at), "end": iso(nr.end_at), "purpose": nr.purpose} if nr else None,
            "conflict_ids": conflict_objs.get(tid, []),
        }

    # ---- поезда и операции
    trains = {}
    for tid, t in model.trains.items():
        ops = model.ops_by_train.get(tid, [])
        if t.status in ("departed", "completed", "cancelled"):
            last = max((aware(o.actual_end) for o in ops if o.actual_end), default=None)
            if not last or now - last > timedelta(hours=2):
                continue
        wl = model.wagons_by_train.get(tid, [])
        cur = next((o for o in ops if o.status == "in_progress"), None)
        nxt = next((o for o in ops if o.status in ("planned", "confirmed")), None)
        trains[tid] = {
            "id": tid, "number": t.number, "kind": t.kind, "priority": t.priority, "status": t.status,
            "status_label": LABELS["train"].get(t.status, t.status), "wagons": t.wagons_count,
            "length_m": model.train_length(t), "track_id": t.current_track_id,
            "origin": t.origin_station_id, "destination": t.destination_station_id,
            "scheduled_arrival": iso(t.scheduled_arrival), "expected_arrival": iso(t.expected_arrival),
            "scheduled_departure": iso(t.scheduled_departure), "expected_departure": iso(t.expected_departure),
            "delay_min": delays.get(tid, 0), "waiting_reason": engine_waiting.get(tid),
            "current_op": {"id": cur.id, "kind": cur.kind, "label": KIND_LABEL.get(cur.kind)} if cur else None,
            "next_op": {"id": nxt.id, "kind": nxt.kind, "label": KIND_LABEL.get(nxt.kind), "start": iso(fc.get(nxt.id, (nxt.planned_start,))[0])} if nxt else None,
            "faulty_wagons": [w.number for w in wl if w.condition == "faulty"],
            "wagon_kinds": _kinds(wl), "pos": train_position(model, t, ops),
            "transfer_request_id": t.transfer_request_id, "conflict_ids": conflict_objs.get(tid, []),
        }
    operations = {}
    w0, w1 = now - timedelta(hours=3), now + timedelta(hours=14)
    for o in model.ops.values():
        if o.status == "cancelled":
            continue
        s, e = fc.get(o.id, (aware(o.actual_start or o.planned_start), aware(o.actual_end or o.planned_end)))
        if e < w0 or s > w1:
            continue
        t = model.trains.get(o.train_id) if o.train_id else None
        operations[o.id] = {
            "id": o.id, "train_id": o.train_id, "train_number": t.number if t else None, "kind": o.kind,
            "kind_label": KIND_LABEL.get(o.kind, o.kind), "seq": o.seq, "track_id": o.track_id,
            "from_track_id": o.from_track_id, "status": o.status, "status_label": LABELS["operation"].get(o.status),
            "planned_start": iso(o.planned_start), "planned_end": iso(o.planned_end),
            "forecast_start": iso(s), "forecast_end": iso(e), "actual_start": iso(o.actual_start),
            "actual_end": iso(o.actual_end), "resource_ids": o.resource_ids or [], "route_nodes": o.route_nodes or [],
            "reserved": o.reserved, "duration_min": o.duration_min, "extra_delay_min": o.extra_delay_min or 0,
            "note": o.note, "priority": t.priority if t else 1,
            "delay_min": round(max(0, (s - aware(o.planned_start)).total_seconds() / 60)) if o.status != "done" else 0,
            "conflict_ids": conflict_objs.get(o.id, []),
        }
    # ---- ресурсы
    resources = {}
    zones = {z.id: z for z in db.execute(select(Zone)).scalars()}
    for rid, r in model.resources.items():
        cur = next((o for o in model.ops.values() if o.status == "in_progress" and rid in (o.resource_ids or [])), None)
        faulty = next((i for i in model.incidents if i.kind == "resource_failure" and i.object_id == rid), None)
        in_shift = model.in_shift(rid, now, now + timedelta(minutes=1))
        status = "faulty" if faulty else ("busy" if cur else ("off_shift" if not in_shift else "available"))
        pos = None
        if r.kind == "shunting_loco":
            pos = loco_position(model, r, cur, trains, zones)
        resources[rid] = {"id": rid, "kind": r.kind, "kind_label": RES_KIND_LABEL.get(r.kind, r.kind), "name": r.name,
                          "zone": r.home_zone_id, "status": status,
                          "status_label": {"faulty": "Неисправен", "busy": "Занят", "off_shift": "Вне смены",
                                           "available": "Свободен"}[status],
                          "current_op": cur.id if cur else None, "pos": pos,
                          "conflict_ids": conflict_objs.get(rid, [])}
    # ---- прочее
    incidents = {}
    for inc in db.execute(select(Incident)).scalars():
        if inc.status == "active" or (inc.end_at and now - aware(inc.end_at) < timedelta(hours=1)):
            incidents[inc.id] = {"id": inc.id, "kind": inc.kind, "title": inc.title, "description": inc.description,
                                 "object_type": inc.object_type, "object_id": inc.object_id, "start": iso(inc.start_at),
                                 "end": iso(inc.end_at), "status": inc.status,
                                 "status_label": LABELS["incident"][inc.status], "severity": inc.severity}
    requests = {}
    for r in db.execute(select(TransferRequest).order_by(TransferRequest.created_at.desc())).scalars():
        lc = r.last_check or {}
        requests[r.id] = {"id": r.id, "number": r.number, "from_station_id": r.from_station_id,
                          "to_station_id": r.to_station_id, "wagons_count": r.wagons_count,
                          "desired_departure": iso(r.desired_departure), "status": r.status,
                          "status_label": LABELS["request"].get(r.status, r.status),
                          "decision": lc.get("decision"), "summary": lc.get("summary"),
                          "check_version": r.last_check_version,
                          "check_stale": (r.last_check_version is not None and r.last_check_version != model.version),
                          "train_id": r.train_id, "updated_at": iso(r.updated_at)}
    recs = {}
    for rec in db.execute(select(Recommendation).where(Recommendation.status.in_(["active", "applied"]))
                          .order_by(Recommendation.computed_real_at.desc()).limit(20)).scalars():
        stale = rec.status == "active" and rec.based_on_version != model.version
        recs[rec.id] = {"id": rec.id, "kind": rec.kind, "title": rec.title, "reason": rec.reason,
                        "affected": rec.affected, "action": rec.action, "effect": rec.effect,
                        "computed_at": iso(rec.computed_at), "computed_real_at": iso(rec.computed_real_at),
                        "based_on_version": rec.based_on_version, "status": "stale" if stale else rec.status,
                        "status_label": "Устарела — требуется перерасчёт" if stale else LABELS["recommendation"][rec.status]}
    pv = db.execute(select(PlanVersion).order_by(PlanVersion.id.desc()).limit(1)).scalars().first()
    plan = None
    if pv:
        plan = {"id": pv.id, "status": "stale" if pv.status == "proposed" and pv.base_state_version != model.version else pv.status,
                "solver": pv.solver, "solver_status": pv.solver_status, "solve_ms": pv.solve_ms,
                "summary": pv.summary, "changes_count": len(pv.changes), "created_at": iso(pv.created_at),
                "model_time": iso(pv.model_time), "trigger": pv.trigger, "base_state_version": pv.base_state_version}
    devs = device_statuses(db, utcnow())
    alerts = {}
    for tid, tr in tracks.items():
        if tr["data_state"] in ("stale", "missing", "invalid", "contradictory"):
            affected = [o["id"] for o in operations.values() if o["track_id"] == tid and o["status"] in ("planned", "confirmed")]
            alerts[f"A-{tid}"] = {"id": f"A-{tid}", "severity": "high", "object_type": "track", "object_id": tid,
                                  "device_id": tr["device_id"], "message": tr["data_message"],
                                  "affected_operations": affected[:10], "data_state": tr["data_state"]}
    for d in devs:
        if d["connection"] == "offline" and d["kind"] in ("switch_sensor", "rfid_reader", "loco_gps", "cargo_equipment", "repair_diag"):
            alerts[f"A-{d['id']}"] = {"id": f"A-{d['id']}", "severity": "medium", "object_type": "device",
                                      "object_id": d["object_id"], "device_id": d["id"],
                                      "message": f"Нет связи с устройством «{d['name']}»: heartbeat не поступает дольше "
                                                 f"{int(max(15, d['period_s'] * 3))} с. Показания этого устройства не используются.", "affected_operations": [],
                                      "data_state": "stale"}
    switches = {}
    obs_sw = {o.object_id: o for o in db.execute(select(Observation).where(Observation.attribute == "switch")).scalars()}
    busy_sw = {}
    for o in model.ops.values():
        if o.status == "in_progress" and o.kind in MOVEMENT_KINDS:
            for sw in o.route_nodes or []:
                busy_sw[sw] = o.id
    for nid, n in model.topo.nodes.items():
        if n["kind"] != "switch":
            continue
        ob = obs_sw.get(nid)
        switches[nid] = {"id": nid, "name": n["name"], "position": ob.value.get("position") if ob else None,
                         "observed_at": iso(ob.observed_at) if ob else None, "route_op": busy_sw.get(nid),
                         "closed": any(i.kind == "switch_failure" and i.object_id == nid for i in model.incidents),
                         "conflict_ids": conflict_objs.get(nid, [])}
    kpi = {"conflicts": len(conflicts), "critical": sum(1 for c in conflicts if c["severity"] == "critical"),
           "delayed_trains": len(det["delays"]), "delays": det["delays"][:10],
           "trains_on_station": sum(1 for t in model.trains.values() if t.status == "on_station"),
           "free_rd_tracks": sum(1 for t in tracks.values() if t["kind"] == "receiving_departure" and t["status"] == "free"),
           "rd_tracks": sum(1 for t in tracks.values() if t["kind"] == "receiving_departure")}
    sim = model.sim
    return {
        "meta": {"model_time": iso(now), "real_time": iso(utcnow()), "speed": sim.speed, "running": sim.running,
                 "scenario": sim.scenario, "scenario_title": SCENARIOS.get(sim.scenario, {}).get("title", sim.scenario),
                 "station_config": sim.station_config, "state_version": model.version, "seed": sim.seed,
                 "station_id": model.sid, "station_name": model.station.name, "timezone": model.station.timezone,
                 "is_demo": True},
        "tracks": tracks, "trains": trains, "operations": operations, "resources": resources,
        "incidents": incidents, "conflicts": {c["id"]: c for c in conflicts}, "recommendations": recs,
        "requests": requests, "alerts": alerts, "switches": switches,
        "index": index, "plan": plan, "kpi": kpi,
        "maintenance": [{"id": m.id, "object_type": m.object_type, "object_id": m.object_id, "start": iso(m.start_at),
                         "end": iso(m.end_at), "reason": m.reason} for m in model.maintenance],
    }


def _kinds(wl):
    out = {}
    for w in wl:
        out[w.kind] = out.get(w.kind, 0) + 1
    return out


def loco_position(model, r: Resource, cur, trains, zones):
    """Координата маневрового локомотива: в голове состава при манёвре, иначе на стоянке."""
    if cur and cur.train_id and cur.train_id in trains and trains[cur.train_id]["pos"]:
        from app.domain.topology import point_at
        p = trains[cur.train_id]["pos"]
        x, y, _ = point_at(p["path"], p["head_s"])
        return {"x": round(x, 1), "y": round(y, 1), "moving": p["moving"]}
    z = zones.get(r.home_zone_id)
    if z:
        idx = int(r.id.split("-")[-1]) if r.id.split("-")[-1].isdigit() else 0
        return {"x": round(z.x + idx * 18, 1), "y": round(z.y, 1), "moving": False}
    return None


SINGLETONS = ("meta", "index", "plan", "kpi", "maintenance")
COLLECTIONS = ("tracks", "trains", "operations", "resources", "incidents", "conflicts", "recommendations",
               "requests", "alerts", "switches")


def diff(prev: dict | None, cur: dict) -> dict:
    """Дельта между двумя состояниями: для коллекций — upsert/remove по идентификатору."""
    if prev is None:
        return {"full": True}
    d = {}
    for k in SINGLETONS:
        if prev.get(k) != cur.get(k):
            d[k] = cur.get(k)
    for k in COLLECTIONS:
        a, b = prev.get(k) or {}, cur.get(k) or {}
        ups = {i: v for i, v in b.items() if a.get(i) != v}
        rem = [i for i in a if i not in b]
        if ups or rem:
            d[k] = {"upsert": ups, "remove": rem}
    return d


def apply_delta(state: dict, d: dict) -> dict:
    """Тот же редьюсер, что во frontend (используется для проверки воспроизведения истории)."""
    out = {k: (dict(v) if isinstance(v, dict) and k in COLLECTIONS else v) for k, v in state.items()}
    for k, v in d.items():
        if k in SINGLETONS:
            out[k] = v
        elif k in COLLECTIONS:
            coll = dict(out.get(k) or {})
            for i in v.get("remove", []):
                coll.pop(i, None)
            coll.update(v.get("upsert", {}))
            out[k] = coll
    return out
