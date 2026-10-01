"""Детектор конфликтов прогнозного плана.

Конфликт — нарушение жёсткого ограничения в прогнозе (с учётом задержек и инцидентов):
пересечение занятости пути, маршрутов (общие стрелки), ресурсов; операция на закрытом пути
или пути без достоверных данных; отправление при ограничении соседа; неспланированное
требование (например, отцепка неисправного вагона). Задержки выводятся отдельно — это не
конфликт, а последствие.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from app.core.timeutil import aware, iso, local_hm
from app.services.forecast import departure_delays, forecast
from app.services.model import FAR, KIND_LABEL, MOVEMENT_KINDS, StationModel, stays_of, with_presence

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
HORIZON = timedelta(hours=12)
DATA_HORIZON = timedelta(hours=6)


def _cid(*parts) -> str:
    return "C-" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def _obj(kind, oid, label):
    return {"type": kind, "id": oid, "label": label}


def detect(model: StationModel, fc: dict | None = None) -> dict:
    fc = fc or forecast(model)
    now = model.now
    horizon_end = now + HORIZON
    conflicts: list[dict] = []
    tl = model.track_label

    def op_dict(o):
        s, e = fc.get(o.id, (aware(o.planned_start), aware(o.planned_end)))
        return {"id": o.id, "kind": o.kind, "track_id": o.track_id, "from_track_id": o.from_track_id,
                "start": s, "end": e, "status": o.status, "resource_ids": o.resource_ids or [],
                "route_nodes": o.route_nodes or [], "train_id": o.train_id, "side": o.side}

    # --- стоянки (занятость путей) по прогнозу
    stays = []
    for tid, ops in model.ops_by_train.items():
        train = model.trains[tid]
        if train.status in ("departed", "completed", "cancelled"):
            continue
        live = with_presence(model, train, [op_dict(o) for o in ops if o.status not in ("done", "cancelled")])
        for idx, st in enumerate(stays_of(live)):
            st = {**st, "ops": [i for i in st["ops"] if not str(i).startswith("presence-")]}
            if st["start"] > horizon_end:
                continue
            started = any(model.ops[i].status == "in_progress" for i in st["ops"] if i in model.ops) or \
                (idx == 0 and train.status == "on_station" and train.current_track_id == st["track_id"])
            stays.append({**st, "train_id": tid, "number": train.number, "priority": train.priority, "started": started})
    for o in model.ops.values():
        if not o.train_id and o.status not in ("done", "cancelled"):
            d = op_dict(o)
            stays.append({"track_id": o.track_id, "start": d["start"], "end": d["end"], "ops": [o.id],
                          "train_id": None, "number": o.note or "операция", "priority": 1,
                          "started": o.status == "in_progress"})

    by_track: dict[str, list] = {}
    for st in stays:
        by_track.setdefault(st["track_id"], []).append(st)
    for track_id, lst in by_track.items():
        lst.sort(key=lambda s: s["start"])
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                a, b = lst[i], lst[j]
                if b["start"] >= a["end"]:
                    break
                if a["train_id"] and a["train_id"] == b["train_id"]:
                    continue
                s, e = max(a["start"], b["start"]), min(a["end"], b["end"])
                conflicts.append({
                    "id": _cid("TRACK", track_id, a["train_id"], b["train_id"]), "type": "track_overlap",
                    "severity": "critical" if min(a["start"], b["start"]) - now < timedelta(hours=1) else "high",
                    "title": f"Пересечение занятости: {model.track_label_lc(track_id)}",
                    "explanation": f"По прогнозу {model.track_label_lc(track_id)} нужен одновременно поезду № {a['number']} "
                                   f"(до {local_hm(a['end'])}) и поезду № {b['number']} (с {local_hm(b['start'])}). "
                                   f"Пересечение {local_hm(s)}–{local_hm(e)}.",
                    "objects": [_obj("track", track_id, tl(track_id))] +
                               [_obj("train", x["train_id"], f"Поезд № {x['number']}") for x in (a, b) if x["train_id"]],
                    "operations": a["ops"] + b["ops"], "start": iso(s), "end": iso(e),
                })
        # закрытия и окна обслуживания
        for st in lst:
            for inc in model.incidents:
                if inc.kind == "track_closure" and inc.object_id == track_id:
                    ie = aware(inc.end_at) or FAR
                    if st["start"] < ie and aware(inc.start_at) < st["end"]:
                        if st["started"]:
                            sev, expl = "medium", (f"Поезд № {st['number']} находится на {model.track_label_lc(track_id)}, который закрыт "
                                                   f"({inc.title}). Новые операции на путь запрещены; отправление — по решению дежурного.")
                        else:
                            sev, expl = "critical", (f"Запланированное занятие {model.track_label_lc(track_id)} поездом № {st['number']} "
                                                     f"({local_hm(st['start'])}–{local_hm(st['end'])}) попадает на закрытие: "
                                                     f"{inc.title} ({local_hm(inc.start_at)}–{local_hm(inc.end_at) if inc.end_at else 'до отмены'}).")
                        conflicts.append({"id": _cid("CLOSED", track_id, st["train_id"], inc.id), "type": "track_closed",
                                          "severity": sev, "title": f"Операция на закрытом пути: {model.track_label_lc(track_id)}",
                                          "explanation": expl,
                                          "objects": [_obj("track", track_id, tl(track_id)), _obj("incident", inc.id, inc.title)] +
                                                     ([_obj("train", st["train_id"], f"Поезд № {st['number']}")] if st["train_id"] else []),
                                          "operations": st["ops"], "start": iso(max(st["start"], aware(inc.start_at))),
                                          "end": iso(min(st["end"], ie)) if ie < FAR else None})
            for mw in model.maintenance:
                if mw.object_type == "track" and mw.object_id == track_id and not st["started"] and \
                        st["start"] < aware(mw.end_at) and aware(mw.start_at) < st["end"]:
                    conflicts.append({"id": _cid("MW", track_id, st["train_id"], mw.id), "type": "maintenance",
                                      "severity": "high", "title": f"Пересечение с окном обслуживания: {model.track_label_lc(track_id)}",
                                      "explanation": f"Занятие поездом № {st['number']} ({local_hm(st['start'])}–{local_hm(st['end'])}) "
                                                     f"пересекается с окном «{mw.reason}» ({local_hm(mw.start_at)}–{local_hm(mw.end_at)}).",
                                      "objects": [_obj("track", track_id, tl(track_id))], "operations": st["ops"],
                                      "start": iso(st["start"]), "end": iso(st["end"])})
            ds = model.data_states.get(track_id)
            if ds and ds["state"] not in ("actual", "not_monitored") and not st["started"] and st["start"] - now < DATA_HORIZON:
                conflicts.append({"id": _cid("DATA", track_id, st["train_id"]), "type": "data_unknown",
                                  "severity": "high", "title": f"Нет достоверных данных: {model.track_label_lc(track_id)}",
                                  "explanation": f"{ds['message']} Зависимая операция: занятие пути поездом № {st['number']} "
                                                 f"с {local_hm(st['start'])} — подтверждение и начало блокируются до получения данных.",
                                  "objects": [_obj("track", track_id, tl(track_id)), _obj("device", ds["device_id"], ds["device_name"])],
                                  "operations": st["ops"], "start": iso(st["start"]), "end": iso(st["end"])})

    # --- маршруты (общие стрелки) и ресурсы
    live_ops = [op_dict(o) for o in model.ops.values()
                if o.status not in ("done", "cancelled") and fc.get(o.id) and fc[o.id][0] < horizon_end]
    moves = [o for o in live_ops if o["kind"] in MOVEMENT_KINDS and o["route_nodes"]]
    moves.sort(key=lambda o: o["start"])
    seen = set()
    for i in range(len(moves)):
        for j in range(i + 1, len(moves)):
            a, b = moves[i], moves[j]
            if b["start"] >= a["end"]:
                break
            if a["train_id"] == b["train_id"]:
                continue
            common = sorted(set(a["route_nodes"]) & set(b["route_nodes"]))
            if not common:
                continue
            key = (a["id"], b["id"])
            if key in seen:
                continue
            seen.add(key)
            na, nb = model.trains[a["train_id"]].number, model.trains[b["train_id"]].number
            conflicts.append({"id": _cid("ROUTE", a["id"], b["id"]), "type": "route_conflict", "severity": "high",
                              "title": "Конфликт маршрутов в горловине",
                              "explanation": f"{KIND_LABEL[a['kind']]} поезда № {na} ({local_hm(a['start'])}–{local_hm(a['end'])}) и "
                                             f"{KIND_LABEL[b['kind']].lower()} поезда № {nb} ({local_hm(b['start'])}–{local_hm(b['end'])}) "
                                             f"используют общие стрелки: {', '.join(c.split('-')[-1] for c in common)}.",
                              "objects": [_obj("switch", c, f"Стрелка {c.split('-')[-1]}") for c in common] +
                                         [_obj("train", a["train_id"], f"Поезд № {na}"), _obj("train", b["train_id"], f"Поезд № {nb}")],
                              "operations": [a["id"], b["id"]], "start": iso(b["start"]), "end": iso(min(a["end"], b["end"]))})
    by_res: dict[str, list] = {}
    for o in live_ops:
        for r in o["resource_ids"]:
            by_res.setdefault(r, []).append(o)
    for rid, lst in by_res.items():
        res = model.resources.get(rid)
        if not res:
            continue
        lst.sort(key=lambda o: o["start"])
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                a, b = lst[i], lst[j]
                if b["start"] >= a["end"]:
                    break
                conflicts.append({"id": _cid("RES", rid, a["id"], b["id"]), "type": "resource_conflict", "severity": "high",
                                  "title": f"Ресурс нужен одновременно: {res.name}",
                                  "explanation": f"{res.name}: {KIND_LABEL[a['kind']].lower()} ({_tn(model, a)}, до {local_hm(a['end'])}) "
                                                 f"и {KIND_LABEL[b['kind']].lower()} ({_tn(model, b)}, с {local_hm(b['start'])}) "
                                                 f"пересекаются. Один ресурс не может выполнять две операции одновременно.",
                                  "objects": [_obj("resource", rid, res.name)], "operations": [a["id"], b["id"]],
                                  "start": iso(b["start"]), "end": iso(a["end"])})
        for o in lst:
            for inc in model.incidents:
                if inc.kind == "resource_failure" and inc.object_id == rid:
                    ie = aware(inc.end_at) or FAR
                    if o["start"] < ie and aware(inc.start_at) < o["end"] and o["status"] != "in_progress":
                        conflicts.append({"id": _cid("RESF", rid, o["id"]), "type": "resource_unavailable", "severity": "high",
                                          "title": f"Ресурс недоступен: {res.name}",
                                          "explanation": f"{KIND_LABEL[o['kind']]} ({_tn(model, o)}, {local_hm(o['start'])}) назначена на "
                                                         f"{res.name}, но ресурс неисправен ({inc.title}).",
                                          "objects": [_obj("resource", rid, res.name), _obj("incident", inc.id, inc.title)],
                                          "operations": [o["id"]], "start": iso(o["start"]), "end": iso(o["end"])})
            if not model.in_shift(rid, o["start"], o["end"]) and o["status"] != "in_progress":
                conflicts.append({"id": _cid("SHIFT", rid, o["id"]), "type": "resource_unavailable", "severity": "medium",
                                  "title": f"Операция вне смены: {res.name}",
                                  "explanation": f"{KIND_LABEL[o['kind']]} ({_tn(model, o)}, {local_hm(o['start'])}–{local_hm(o['end'])}) "
                                                 f"выходит за пределы смены ресурса «{res.name}».",
                                  "objects": [_obj("resource", rid, res.name)], "operations": [o["id"]],
                                  "start": iso(o["start"]), "end": iso(o["end"])})

    # --- соседние станции, неспланированные требования, неисправные вагоны
    for o in live_ops:
        if o["kind"] == "departure" and o["status"] != "in_progress":
            train = model.trains[o["train_id"]]
            dest = train.destination_station_id
            for inc in model.incidents:
                if inc.kind == "neighbor_restriction" and inc.object_id == dest:
                    ie = aware(inc.end_at) or FAR
                    if o["start"] < ie and aware(inc.start_at) <= o["end"]:
                        conflicts.append({"id": _cid("NB", o["id"], inc.id), "type": "neighbor_restriction", "severity": "high",
                                          "title": f"Отправление при ограничении соседней станции",
                                          "explanation": f"Отправление поезда № {train.number} в {local_hm(o['start'])} на "
                                                         f"{model.neighbors[dest].name if dest in model.neighbors else dest} попадает в ограничение "
                                                         f"«{inc.title}» до {local_hm(inc.end_at) if inc.end_at else 'отмены'}.",
                                          "objects": [_obj("train", train.id, f"Поезд № {train.number}"), _obj("incident", inc.id, inc.title)],
                                          "operations": [o["id"]], "start": iso(o["start"]), "end": iso(o["end"])})
    for o in model.ops.values():
        if not o.reserved and o.status in ("planned", "confirmed"):
            conflicts.append({"id": _cid("UNPL", o.id), "type": "unplanned", "severity": "high",
                              "title": f"Требуется планирование: {KIND_LABEL.get(o.kind, o.kind).lower()}",
                              "explanation": f"{o.note or KIND_LABEL.get(o.kind)}: операция ещё не размещена во времени и "
                                             f"ресурсах. Пока она не спланирована, зависящие операции не начнутся.",
                              "objects": [_obj("track", o.track_id, tl(o.track_id))] +
                                         ([_obj("train", o.train_id, f"Поезд № {model.trains[o.train_id].number}")] if o.train_id else []),
                              "operations": [o.id], "start": iso(fc.get(o.id, (aware(o.planned_start),))[0]), "end": None})
    for tid, wl in model.wagons_by_train.items():
        bad = [w for w in wl if w.condition in ("faulty", "restricted")]
        t = model.trains.get(tid)
        if bad and t and t.status not in ("departed", "completed"):
            has_unc = any(o.kind == "uncoupling" and o.status != "cancelled" for o in model.ops_by_train.get(tid, []))
            if not has_unc:
                conflicts.append({"id": _cid("FW", tid), "type": "faulty_wagon", "severity": "critical",
                                  "title": f"Неисправный вагон в составе поезда № {t.number}",
                                  "explanation": (f"Вагон № {bad[0].number}: временное ограничение до проверки сообщения о "
                                                  f"предположительно критическом дефекте. Отправление состава до решения моделью не допускается."
                                                  if bad[0].condition == "restricted" else
                                                  f"Вагон № {bad[0].number} неисправен. Отправление состава с неисправным вагоном "
                                                  f"моделью не допускается до устранения и контрольного осмотра (ремонт без отцепки) "
                                                  f"или отцепки — требуется решение диспетчера."),
                                  "objects": [_obj("train", tid, f"Поезд № {t.number}"), _obj("wagon", bad[0].id, f"Вагон № {bad[0].number}")],
                                  "operations": [o.id for o in model.ops_by_train.get(tid, []) if o.kind == "departure"],
                                  "start": iso(now), "end": None})

    uniq = {c["id"]: c for c in conflicts}
    conflicts = sorted(uniq.values(), key=lambda c: (SEVERITY_ORDER[c["severity"]], c.get("start") or ""))
    for c in conflicts:
        c["detected_at"] = iso(now)
        c["severity_label"] = {"critical": "Критично", "high": "Высокая", "medium": "Средняя", "low": "Низкая"}[c["severity"]]
    delays = departure_delays(model, fc)
    delay_items = []
    for tid, d in sorted(delays.items(), key=lambda kv: -kv[1]):
        if d >= 5:
            t = model.trains[tid]
            if t.status in ("departed", "completed"):
                continue
            delay_items.append({"train_id": tid, "number": t.number, "delay_min": round(d), "priority": t.priority})
    return {"conflicts": conflicts, "delays": delay_items, "forecast": fc}


def _tn(model, o):
    t = model.trains.get(o["train_id"]) if o.get("train_id") else None
    return f"поезд № {t.number}" if t else "операция"


def route_conflict_count(conflicts: list[dict]) -> int:
    return sum(1 for c in conflicts if c["type"] in ("route_conflict", "track_overlap", "track_closed"))
