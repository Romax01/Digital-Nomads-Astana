"""Проверка возможности приёма состава по заявке между станциями.

Каждое ограничение проверяется отдельно и возвращает пункт с единицей измерения,
периодом, источником данных, правилом и статусом: ok | warning | fail | insufficient_data.
Виды ограничений не смешиваются: месячный план (цель или жёсткая квота), перерабатывающая
способность за сутки, пропускная способность входа за час, одновременная вместимость путей
на интервал, ресурсы, ограничения соседней станции, данные датчиков.

Все времена и объяснения вычисляются из данных; готовых текстов-ответов нет.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from app.core import metrics
from app.core.timeutil import aware, iso, local_hm, tz, utcnow
from app.models import Station, TransferRequest
from app.services.model import FAR, IntervalBook, StationModel, describe_block
from app.services.placement import Placer, TrainSpec

STATUS_RANK = {"ok": 0, "warning": 1, "insufficient_data": 2, "fail": 3}


def train_length_for_request(model: StationModel, req) -> tuple[float | None, str]:
    if req.train_length_m:
        return float(req.train_length_m), "указана в заявке"
    lengths = model.cfg["processing"].get("wagon_lengths_m", {})
    if req.wagon_kind and req.wagon_kind in lengths:
        loco = model.cfg["processing"].get("default_loco_length_m", 34.0)
        L = req.wagons_count * lengths[req.wagon_kind] + loco
        return round(L, 1), f"{req.wagons_count} ваг. × {lengths[req.wagon_kind]} м + локомотив {loco:g} м"
    return None, "нет общей длины и длины вагона"


def _item(code, title, status, message, *, unit=None, period=None, source=None, rule=None, values=None):
    return {"code": code, "title": title, "status": status, "message": message, "unit": unit,
            "period": period, "source": source, "rule": rule, "values": values or {}}


def _rule_meta(model: StationModel, code: str) -> dict:
    r = model.rules.get(code)
    if not r:
        return {}
    return {"unit": r.unit, "period": r.period, "source": r.source, "rule": r.rule_text}


def plan_figures(model: StationModel, month_start: datetime, month_end: datetime) -> dict:
    """Факт и согласованные будущие объёмы за месяц, вагонов."""
    plan = model.plan
    done = 0
    agreed = 0
    for t in model.trains.values():
        if t.kind not in ("freight", "transfer") or t.status == "cancelled":
            continue
        ops = model.ops_by_train.get(t.id, [])
        insp = next((o for o in ops if o.kind == "inspection"), None)
        if insp is None:
            continue
        when = aware(insp.actual_end or insp.planned_end)
        if not (month_start <= when < month_end):
            continue
        if insp.status == "done":
            done += t.wagons_count
        else:
            agreed += t.wagons_count
    return {"target": plan.target_wagons if plan else None, "fact": (plan.actual_base_wagons if plan else 0) + done,
            "agreed": agreed, "policy": plan.policy if plan else "soft"}


def _month_bounds(dt: datetime):
    loc = dt.astimezone(tz())
    ms = loc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    me = (ms + timedelta(days=32)).replace(day=1)
    return ms, me


def check_static_rules(model: StationModel, req, arrival: datetime, length: float | None, wagons: int) -> list[dict]:
    items = []
    # 1. месячный план — целевой показатель или жёсткая квота
    ms, me = _month_bounds(arrival)
    pf = plan_figures(model, ms, me)
    meta = _rule_meta(model, "MONTHLY_PLAN")
    if pf["target"] is None:
        items.append(_item("MONTHLY_PLAN", "Месячный план обработки", "insufficient_data",
                           "План месяца не задан — оценить выполнение невозможно.", **meta))
    else:
        projected = pf["fact"] + pf["agreed"] + wagons
        over = projected - pf["target"]
        vals = {"target": pf["target"], "fact": pf["fact"], "agreed": pf["agreed"], "request": wagons,
                "projected": projected, "policy": pf["policy"], "month": ms.strftime("%m.%Y")}
        if over <= 0:
            items.append(_item("MONTHLY_PLAN", "Месячный план обработки", "ok",
                               f"С учётом заявки: {projected} из {pf['target']} ваг. (факт {pf['fact']}, "
                               f"согласовано {pf['agreed']}). Превышения нет.", values=vals, **meta))
        elif pf["policy"] == "hard_quota":
            items.append(_item("MONTHLY_PLAN", "Месячный план обработки", "fail",
                               f"Жёсткая квота: с заявкой будет {projected} ваг. при квоте {pf['target']} "
                               f"(превышение на {over}). Политика «жёсткая квота» запрещает новые заявки.",
                               values=vals, **meta))
        else:
            items.append(_item("MONTHLY_PLAN", "Месячный план обработки", "warning",
                               f"План месяца {'уже выполнен' if pf['fact'] >= pf['target'] else 'будет превышен'}: "
                               f"с заявкой {projected} из {pf['target']} ваг. (превышение на {over}). "
                               f"Политика «целевой показатель»: заявка допускается с предупреждением.",
                               values=vals, **meta))
    # 2. перерабатывающая способность за сутки
    rule = model.rules.get("DAILY_PROCESSING")
    if rule and rule.active and rule.value:
        loc = arrival.astimezone(tz())
        d0 = loc.replace(hour=0, minute=0, second=0, microsecond=0)
        d1 = d0 + timedelta(days=1)
        day_wagons = 0
        for t in model.trains.values():
            if t.kind in ("freight", "transfer") and t.status != "cancelled" and t.expected_arrival and \
                    d0 <= aware(t.expected_arrival) < d1:
                day_wagons += t.wagons_count
        total = day_wagons + wagons
        st = "ok" if total <= rule.value else ("fail" if rule.policy == "hard" else "warning")
        items.append(_item("DAILY_PROCESSING", rule.name, st,
                           f"За сутки {d0.strftime('%d.%m')} в переработку: {day_wagons} ваг. + заявка {wagons} = "
                           f"{total} из {rule.value:.0f} ваг./сут." + ("" if st == "ok" else " Превышение перерабатывающей способности."),
                           values={"planned": day_wagons, "request": wagons, "limit": rule.value}, **_rule_meta(model, "DAILY_PROCESSING")))
    # 3. пропускная способность входа (поездов в час)
    side = model.neighbors[req.from_station_id].config.get("side", "west") if req.from_station_id in model.neighbors else "west"
    rule = model.rules.get(f"ENTRY_THROUGHPUT_{side.upper()}")
    if rule and rule.active and rule.value:
        w0, w1 = arrival - timedelta(minutes=30), arrival + timedelta(minutes=30)
        cnt = 0
        for t in model.trains.values():
            if t.status == "cancelled" or t.arrival_side != side:
                continue
            arr = next((o for o in model.ops_by_train.get(t.id, []) if o.kind == "arrival"), None)
            if arr and arr.status != "done" and w0 <= aware(arr.forecast_start or arr.planned_start) < w1:
                cnt += 1
        total = cnt + 1
        st = "ok" if total <= rule.value else "fail"
        items.append(_item(rule.code, rule.name, st,
                           f"Прибытий с этой стороны в окне {local_hm(w0)}–{local_hm(w1)}: {cnt} + заявка = {total} "
                           f"при норме {rule.value:.0f} поезд./ч." + ("" if st == "ok" else " Пропускная способность входа исчерпана."),
                           values={"count": cnt, "limit": rule.value}, **_rule_meta(model, rule.code)))
    # 4. данные о длине
    if length is None:
        items.append(_item("TRAIN_LENGTH", "Длина состава", "insufficient_data",
                           "Недостаточно данных: в заявке нет длины состава и вида вагонов с известной длиной. "
                           "Проверка соответствия полезной длине путей невозможна.",
                           unit="м", period="на момент прибытия", source="Заявка",
                           rule="Длина состава + запас ≤ полезной длины пути. Количество вагонов не заменяет длину."))
    return items


def neighbor_checks(model: StationModel, book: IntervalBook, req, departure: datetime, arrival: datetime) -> list[dict]:
    items = []
    origin: Station | None = model.neighbors.get(req.from_station_id)
    if origin is None:
        items.append(_item("ORIGIN", "Станция отправления", "insufficient_data",
                           f"Станция {req.from_station_id} не найдена в модели.", source="Справочник станций"))
        return items
    ocfg = origin.config
    restr = book.conflicts(f"neighbor:{origin.id}", departure, departure + timedelta(minutes=10))
    if restr:
        items.append(_item("ORIGIN_RESTRICTION", f"Ограничение станции {origin.name}", "fail",
                           f"Отправление со станции {origin.name} в {local_hm(departure)} невозможно: "
                           f"{describe_block(restr[0])}.", source="Состояние соседней станции (упрощённое)"))
    if ocfg.get("locomotives_available", 1) <= 0:
        items.append(_item("ORIGIN_LOCO", "Поездной локомотив", "fail",
                           f"На станции {origin.name} нет свободного поездного локомотива.",
                           unit="локомотивов", source="Состояние соседней станции (упрощённое)"))
    return items


def _spec_for_request(model: StationModel, req, arrival: datetime, length: float | None, wagons: int) -> TrainSpec:
    tpl = model.cfg["processing"]["templates"][model.cfg["processing"]["transfer_template"]]
    side = model.neighbors[req.from_station_id].config.get("side", "west") if req.from_station_id in model.neighbors else "west"
    return TrainSpec(train_id=f"REQ-{req.id}", number=f"заявка {req.number}", kind="transfer",
                     priority=req.priority or 2, length_m=length, side_in=side,
                     side_out="east" if side == "west" else "west", template=tpl, arrival=arrival, wagons=wagons)


def place_request(model: StationModel, book: IntervalBook, req, arrival: datetime, length, wagons, ignore_train=None):
    placer = Placer(model, book, wait_max_min=60, ignore_train=ignore_train)
    return placer.place(_spec_for_request(model, req, arrival, length, wagons), arrival_exact=True)


def travel_min(model: StationModel, from_id: str) -> int:
    n = model.neighbors.get(from_id)
    return int(n.config.get("travel_min", 90)) if n else 90


def summarize_failure(arrival: datetime, wagons: int, res, model: StationModel | None = None) -> str:
    """Сводка отказа из причин по путям-кандидатам (вычисляется, а не задаётся текстом)."""
    reasons = res.track_reasons
    by_code: dict[str, list[str]] = {}
    for tid, r in reasons.items():
        by_code.setdefault(r["code"], []).append(tid)
    head = f"Приём {wagons} ваг. в {local_hm(arrival)} недоступен"
    usable = {c: v for c, v in by_code.items() if c not in ("LENGTH", "INCOMPATIBLE")}
    if not usable:
        return f"{head}: нет пути достаточной полезной длины."
    if set(usable) <= {"OCCUPIED", "CLOSED", "DATA"} and "DATA" not in usable:
        return f"{head}: нет подходящего свободного пути на время приёма и обработки."

    def nums(ids):
        if model is None:
            return ""
        return ", ".join(model.track_rows[t].number for t in sorted(ids, key=lambda x: model.track_rows[x].number.zfill(3)))
    parts = []
    if "RESOURCE" in usable or "RESOURCE_NONE" in usable:
        ids = usable.get("RESOURCE", []) + usable.get("RESOURCE_NONE", [])
        parts.append(f"на путях {nums(ids)} нет свободных ресурсов обработки на нужный интервал")
    if "OCCUPIED" in usable or "CLOSED" in usable:
        parts.append(f"пути {nums(usable.get('OCCUPIED', []) + usable.get('CLOSED', []))} заняты или закрыты")
    if "DATA" in usable:
        parts.append(f"по путям {nums(usable['DATA'])} нет достоверных данных датчиков")
    if "ROUTE" in usable:
        parts.append(f"маршрут приёма на пути {nums(usable['ROUTE'])} занят")
    return f"{head}: " + "; ".join(parts) + "."


def check_request(model: StationModel, req: TransferRequest, *, departure: datetime | None = None,
                  with_alternatives: bool = True, book: IntervalBook | None = None) -> dict:
    t0 = time.perf_counter()
    departure = aware(departure or req.desired_departure)
    tmin = travel_min(model, req.from_station_id)
    arrival = departure + timedelta(minutes=tmin)
    length, length_src = train_length_for_request(model, req)
    book = book or model.build_book()
    ignore = req.train_id  # при перепроверке подтверждённой заявки собственные резервы не мешают
    items = check_static_rules(model, req, arrival, length, req.wagons_count)
    items += neighbor_checks(model, book, req, departure, arrival)
    if departure < model.now:
        items.append(_item("TIME_PAST", "Время отправления", "fail",
                           f"Время отправления {local_hm(departure)} уже прошло (модельное время {local_hm(model.now)}).",
                           unit="время", source="Модельные часы"))
    placement = place_request(model, book, req, arrival, length, req.wagons_count, ignore_train=ignore)
    track_items = []
    for tid, reason in placement.track_reasons.items():
        track_items.append({"track_id": tid, "label": model.track_label(tid), **reason})
    assignment = None
    if placement.ok:
        ops = placement.ops
        stays = [o for o in ops if o.kind in ("arrival", "shunting")]
        main_track = ops[0].track_id
        free_now = not book.conflicts(f"track:{main_track}", model.now, model.now + timedelta(minutes=1), ignore_train=ignore)
        occ = book.conflicts(f"track:{main_track}", model.now, ops[0].start, ignore_train=ignore)
        note = ""
        if occ:
            last = max(occ, key=lambda e: e.end)
            note = f" Сейчас путь занят ({describe_block(last)}), освободится к {local_hm(last.end)} — до начала приёма."
        items.append(_item("TIME_WINDOW", "Путь и интервал приёма", "ok",
                           f"{model.track_label(main_track)} свободен на весь интервал приёма и обработки "
                           f"{local_hm(ops[0].start)}–{local_hm(_stay_end(ops, main_track))}.{note}",
                           values={"track_id": main_track, "free_now": free_now}, **_rule_meta(model, "TIME_WINDOW")))
        res_list = sorted({r for o in ops for r in o.resource_ids})
        items.append(_item("RESOURCES", "Ресурсы обработки", "ok",
                           "Назначены: " + ", ".join(model.resources[r].name for r in res_list) + "." if res_list
                           else "Ресурсы не требуются.", values={"resources": res_list}, **_rule_meta(model, "RESOURCES")))
        assignment = {"ops": [o.as_dict() for o in ops],
                      "tracks": [o.track_id for o in stays],
                      "resources": res_list}
    else:
        msg = summarize_failure(arrival, req.wagons_count, placement, model)
        st = "insufficient_data" if (length is None or (placement.failure_codes == {"INSUFFICIENT_DATA"})) else "fail"
        items.append(_item("TIME_WINDOW", "Путь и интервал приёма", st, msg,
                           values={"tracks": track_items}, **_rule_meta(model, "TIME_WINDOW")))

    worst = max((STATUS_RANK[i["status"]] for i in items), default=0)
    decision = {0: "available", 1: "available_with_warnings", 2: "insufficient_data", 3: "unavailable"}[worst]
    if decision == "insufficient_data" and any(i["status"] == "fail" for i in items):
        decision = "unavailable"
    window = None
    alternatives = []
    if decision in ("unavailable",) and length is not None and with_alternatives:
        from app.services.alternatives import build_alternatives
        alternatives, window = build_alternatives(model, book, req, departure, arrival, length, items)
    summary = _summary(decision, req, arrival, items, placement, window, model)
    if window:
        window = {k: v for k, v in window.items() if k != "arrival_dt"}
    missing = [i["message"] for i in items if i["status"] == "insufficient_data"] + placement.insufficient
    elapsed = (time.perf_counter() - t0) * 1000
    metrics.observe("check_ms", elapsed)
    return {
        "decision": decision,
        "decision_label": {"available": "Приём возможен", "available_with_warnings": "Приём возможен с предупреждениями",
                           "unavailable": "Приём недоступен", "insufficient_data": "Недостаточно данных"}[decision],
        "summary": summary,
        "departure": iso(departure), "arrival": iso(arrival), "travel_min": tmin,
        "train_length_m": length, "train_length_source": length_src, "wagons": req.wagons_count,
        "items": items, "tracks": track_items, "assignment": assignment,
        "nearest_window": window, "alternatives": alternatives, "missing_data": missing,
        "computed_at": iso(model.now), "computed_real_at": iso(utcnow()), "state_version": model.version,
        "calc_ms": round(elapsed, 1),
        "assumptions": [
            f"Время хода от станции отправления — {tmin} мин (норматив модели).",
            "Интервал занятия пути: от прибытия до окончания обработки и вытягивания + технологический запас.",
            "Схема станции, нормы и показатели демонстрационные.",
        ],
    }


def _stay_end(ops, track):
    end = None
    for o in ops:
        if o.track_id == track or o.from_track_id == track:
            end = o.end
    return end or ops[-1].end


def _summary(decision, req, arrival, items, placement, window, model):
    if decision in ("available", "available_with_warnings"):
        tr = placement.ops[0].track_id
        s = f"Приём {req.wagons_count} ваг. в {local_hm(arrival)} возможен: {model.track_label_lc(tr)}, " \
            f"обработка до {local_hm(_stay_end(placement.ops, tr))}."
        warns = [i for i in items if i["status"] == "warning"]
        if warns:
            s += " Предупреждение: " + warns[0]["message"]
        return s
    fails = [i for i in items if i["status"] == "fail"]
    if not fails:
        miss = [i for i in items if i["status"] == "insufficient_data"]
        return "Недостаточно данных для решения: " + (miss[0]["message"] if miss else "проверьте исходные данные.")
    main = next((i for i in fails if i["code"] == "TIME_WINDOW"), fails[0])
    s = main["message"]
    if window:
        s = s.rstrip(".") + f". Ближайшее допустимое окно — {local_hm(window['arrival_dt'])}."
    elif main["code"] == "TIME_WINDOW":
        s += " Допустимого окна в ближайшие 12 ч не найдено."
    return s
