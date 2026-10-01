"""Проверенные альтернативы при отказе в приёме.

Предлагаются только варианты, прошедшие ту же проверку ограничений, что и исходная заявка:
  * перенос отправления на ближайшее допустимое окно;
  * другая совместимая станция (по упрощённой модели соседа);
  * изменение очереди операций (перестановка менее приоритетного поезда на другой путь);
  * разделение партии (если разрешено заявкой и хватает ресурсов).
Для каждого варианта считаются последствия: задержка, изменение загрузки, затронутые операции.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.core.timeutil import aware, iso, local_hm
from app.services.model import IntervalBook, StationModel
from app.services.placement import Placer, TrainSpec, commit_to_book

STEP = timedelta(minutes=5)


def _static_ok(model, req, arrival, length, wagons) -> bool:
    from app.services.checker import check_static_rules
    return all(i["status"] != "fail" for i in check_static_rules(model, req, arrival, length, wagons))


def _occupancy_minutes(ops, track_id) -> int:
    s = min(o.start for o in ops if o.track_id == track_id)
    e = max(o.end for o in ops if o.track_id == track_id or o.from_track_id == track_id)
    return int((e - s).total_seconds() // 60)


def nearest_window(model: StationModel, book: IntervalBook, req, arrival: datetime, length, wagons,
                   horizon_h: int = 12):
    from app.services.checker import neighbor_checks, place_request, travel_min
    tmin = travel_min(model, req.from_station_id)
    t = arrival.replace(second=0, microsecond=0)
    t = t + timedelta(minutes=(-t.minute) % 5)
    if t <= arrival:
        t += STEP
    end = arrival + timedelta(hours=horizon_h)
    while t <= end:
        dep = t - timedelta(minutes=tmin)
        if dep >= model.now:
            r = place_request(model, book, req, t, length, wagons, ignore_train=req.train_id)
            if r.ok and not [i for i in neighbor_checks(model, book, req, dep, t) if i["status"] == "fail"] \
                    and _static_ok(model, req, t, length, wagons):
                return t, dep, r
        t += STEP
    return None, None, None


def build_alternatives(model: StationModel, book: IntervalBook, req, departure: datetime, arrival: datetime,
                       length: float, items: list[dict]):
    alts = []
    window = None
    hard_static_fail = [i for i in items if i["status"] == "fail" and i["code"] in ("MONTHLY_PLAN",)]
    if hard_static_fail:
        # жёсткая квота: время не поможет — предлагаем только другую станцию
        pass
    else:
        t, dep, r = nearest_window(model, book, req, arrival, length, req.wagons_count)
        if t:
            delay = int((t - arrival).total_seconds() // 60)
            tr = r.ops[0].track_id
            window = {"arrival": iso(t), "departure": iso(dep), "track_id": tr, "delay_min": delay, "arrival_dt": t}
            alts.append({
                "type": "postpone", "title": f"Перенести отправление на {local_hm(dep)}",
                "description": f"Прибытие в {local_hm(t)} на {model.track_label_lc(tr)}; весь интервал приёма "
                               f"и обработки проверен.",
                "verified": True,
                "effect": {"delay_min": delay,
                           "load_change": f"{model.track_label(tr)}: +{_occupancy_minutes(r.ops, tr)} мин занятости "
                                          f"с {local_hm(r.ops[0].start)}",
                           "affected_operations": []},
                "action": {"type": "postpone", "departure": iso(dep)},
            })
    alts += _other_station(model, book, req, departure, length)
    if not hard_static_fail:
        alts += _reorder(model, book, req, arrival, length)
        if req.split_allowed and req.wagons_count >= 2:
            alts += _split(model, book, req, arrival, length)
    return alts, window


def _other_station(model: StationModel, book: IntervalBook, req, departure: datetime, length: float) -> list[dict]:
    out = []
    origin = model.neighbors.get(req.from_station_id)
    if not origin:
        return out
    for nid, n in model.neighbors.items():
        if nid == req.from_station_id:
            continue
        c = n.config
        travel = (origin.config.get("travel_min_to") or {}).get(nid)
        if travel is None or "transfer" not in c.get("accepts", []):
            continue
        reasons = []
        if c.get("max_train_length_m") and length > c["max_train_length_m"]:
            reasons.append(f"длина состава {length:.0f} м больше допустимой {c['max_train_length_m']} м")
        arr = departure + timedelta(minutes=travel)
        proc = timedelta(minutes=c.get("processing_min", 90))
        restr = book.conflicts(f"neighbor:{nid}", arr, arr + proc)
        if restr:
            reasons.append("действует ограничение приёма")
        free_tracks = 0
        for occ in (c.get("occupancy") or []):
            busy = any(aware(datetime.fromisoformat(a)) < arr + proc and arr < aware(datetime.fromisoformat(b))
                       for a, b in occ)
            if not busy:
                free_tracks += 1
        if not c.get("occupancy"):
            free_tracks = c.get("receiving_tracks", 0)
        if free_tracks == 0:
            reasons.append("нет свободного приёмного пути на интервал")
        if reasons:
            continue
        own_travel = int(origin.config.get("travel_min", 90))
        out.append({
            "type": "other_station", "title": f"Направить на станцию {n.name}",
            "description": f"Прибытие на {n.name} в {local_hm(arr)}; свободных приёмных путей на интервал обработки: "
                           f"{free_tracks}. Проверено по упрощённой модели соседней станции.",
            "verified": True,
            "effect": {"delay_min": 0, "extra_travel_min": travel - own_travel,
                       "load_change": f"Загрузка {model.station.name} не меняется; у {n.name}: +1 состав на "
                                      f"{int(proc.total_seconds() // 60)} мин",
                       "affected_operations": []},
            "action": {"type": "other_station", "station_id": nid},
        })
    return out


def _train_spec_from_ops(model: StationModel, train) -> tuple[TrainSpec, list]:
    ops = model.ops_by_train.get(train.id, [])
    kinds = []
    for o in ops:
        step = {"kind": o.kind, "duration": o.duration_min, "requires": list(o.requirements or [])}
        if o.kind == "arrival" or o.kind == "shunting":
            step["group"] = model.track_rows[o.track_id].kind
        kinds.append(step)
    dep = next((o for o in ops if o.kind == "departure"), None)
    spec = TrainSpec(train_id=train.id, number=train.number, kind=train.kind, priority=train.priority,
                     length_m=model.train_length(train), side_in=train.arrival_side, side_out=train.departure_side,
                     template=kinds, arrival=aware(ops[0].planned_start) if ops else model.now,
                     departure_not_before=aware(train.scheduled_departure) if dep else None,
                     wagons=train.wagons_count)
    return spec, ops


def _reorder(model: StationModel, book: IntervalBook, req, arrival: datetime, length: float) -> list[dict]:
    """Освободить путь, переставив менее приоритетный не начатый поезд на другой путь."""
    from app.services.checker import place_request
    out = []
    tried = set()
    for tid, t in model.track_rows.items():
        if t.kind != "receiving_departure":
            continue
        for e in book.conflicts(f"track:{tid}", arrival, arrival + timedelta(hours=3), kinds={"reservation"}):
            x = model.trains.get(e.meta.get("train_id"))
            if not x or x.id in tried or x.priority > (req.priority or 2):
                continue
            ops = model.ops_by_train.get(x.id, [])
            if not ops or any(o.status in ("in_progress", "done") for o in ops):
                continue
            tried.add(x.id)
            b2 = IntervalBook()
            b2.data = {k: [en for en in v if en.meta.get("train_id") != x.id] for k, v in book.data.items()}
            r = place_request(model, b2, req, arrival, length, req.wagons_count, ignore_train=req.train_id)
            if not r.ok:
                continue
            commit_to_book(model, b2, f"REQ-{req.id}", req.number, r.ops)
            spec, old_ops = _train_spec_from_ops(model, x)
            r2 = Placer(model, b2, wait_max_min=45).place(spec, arrival_exact=True)
            if not r2.ok:
                continue
            old_dep = next((o for o in old_ops if o.kind == "departure"), None)
            new_dep = next((o for o in r2.ops if o.kind == "departure"), None)
            delay = int((new_dep.start - aware(old_dep.planned_start)).total_seconds() // 60) if old_dep and new_dep else 0
            if delay > 45:
                continue
            old_track = old_ops[0].track_id
            new_track = r2.ops[0].track_id
            changed = []
            for o_old, o_new in zip(old_ops, r2.ops):
                if o_old.track_id != o_new.track_id or aware(o_old.planned_start) != o_new.start:
                    changed.append({"operation_id": o_old.id, "train": x.number, "kind": o_old.kind,
                                    "from": f"{model.track_label(o_old.track_id)} {local_hm(o_old.planned_start)}",
                                    "to": f"{model.track_label(o_new.track_id)} {local_hm(o_new.start)}"})
            out.append({
                "type": "reorder",
                "title": f"Изменить очередь: поезд № {x.number} — с {model.track_label_lc(old_track)} на "
                         f"{model.track_label_lc(new_track)}",
                "description": f"Освобождает {model.track_label_lc(r.ops[0].track_id)} для приёма в "
                               f"{local_hm(arrival)}. Поезд № {x.number} (приоритет {x.priority}) не начат; "
                               f"обе цепочки операций проверены.",
                "verified": True,
                "effect": {"delay_min": max(0, delay), "affected_train": x.number,
                           "load_change": f"{model.track_label(new_track)} занят поездом № {x.number} вместо "
                                          f"{model.track_label_lc(old_track)}",
                           "affected_operations": changed},
                "action": {"type": "reorder", "train_id": x.id,
                           "ops": [{"operation_id": o_old.id, "track_id": o_new.track_id,
                                    "from_track_id": o_new.from_track_id, "start": iso(o_new.start),
                                    "resource_ids": o_new.resource_ids, "route_nodes": o_new.route_nodes}
                                   for o_old, o_new in zip(old_ops, r2.ops)]},
                "requires_role": "station_dispatcher",
            })
            if len(out) >= 2:
                return out
    return out


def _split(model: StationModel, book: IntervalBook, req, arrival: datetime, length: float) -> list[dict]:
    from app.services.checker import place_request, train_length_for_request
    origin = model.neighbors.get(req.from_station_id)
    if origin and origin.config.get("locomotives_available", 1) < 2:
        return []
    n1 = math.ceil(req.wagons_count / 2)
    n2 = req.wagons_count - n1
    per_wagon = (length - model.cfg["processing"].get("default_loco_length_m", 34)) / req.wagons_count
    loco = model.cfg["processing"].get("default_loco_length_m", 34)
    l1, l2 = round(n1 * per_wagon + loco, 1), round(n2 * per_wagon + loco, 1)
    b = book.copy()
    r1 = place_request(model, b, req, arrival, l1, n1, ignore_train=req.train_id)
    if not r1.ok:
        return []
    commit_to_book(model, b, f"REQ-{req.id}-1", f"{req.number}/1", r1.ops)
    t = arrival + timedelta(minutes=15)
    while t <= arrival + timedelta(hours=4):
        r2 = place_request(model, b, req, t, l2, n2, ignore_train=req.train_id)
        if r2.ok:
            delay = int((t - arrival).total_seconds() // 60)
            return [{
                "type": "split",
                "title": f"Разделить партию: {n1} ваг. в {local_hm(arrival)} и {n2} ваг. в {local_hm(t)}",
                "description": f"Часть 1 ({l1:.0f} м) — {model.track_label_lc(r1.ops[0].track_id)}, часть 2 "
                               f"({l2:.0f} м) — {model.track_label_lc(r2.ops[0].track_id)}. Требуется 2 поездных "
                               f"локомотива на станции отправления (доступно: {origin.config.get('locomotives_available')}).",
                "verified": True,
                "effect": {"delay_min": delay, "load_change": "Две операции приёма вместо одной",
                           "affected_operations": []},
                "action": {"type": "split", "parts": [
                    {"wagons": n1, "departure": iso(aware(req.desired_departure))},
                    {"wagons": n2, "departure": iso(aware(req.desired_departure) + (t - arrival))}]},
            }]
        t += STEP
    return []
