"""Размещение цепочки операций поезда: путь, время, ресурсы, маршрут.

Детерминированный алгоритм «самое раннее допустимое размещение» (earliest-fit) с перебором
путей в порядке best-fit (наименьший подходящий по длине путь). Используется:
  * проверкой заявки (прибытие строго в заданное время, ожидание внутри станции ограничено);
  * поиском ближайшего окна;
  * начальным планом и эвристическим планировщиком.

Жёсткие ограничения (никогда не нарушаются): совместимость пути, длина, отсутствие
пересечений резервов/закрытий/окон обслуживания, недостоверные данные пути, маршрут
по топологии и занятость стрелок, смены и занятость ресурсов, технологическая
последовательность операций.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.core.timeutil import ceil_to, local_hm
from app.services.model import FAR, KIND_LABEL, MOVEMENT_KINDS, RES_KIND_LABEL, Entry, IntervalBook, StationModel, describe_block

MIN = timedelta(minutes=1)


@dataclass
class TrainSpec:
    train_id: str
    number: str
    kind: str                     # freight | transfer | passenger
    priority: int
    length_m: float | None
    side_in: str
    side_out: str
    template: list[dict]
    arrival: datetime
    departure_not_before: datetime | None = None
    wagons: int = 0


@dataclass
class PlacedOp:
    kind: str
    seq: int
    track_id: str
    start: datetime
    end: datetime
    duration_min: int
    from_track_id: str | None = None
    side: str | None = None
    requirements: list = field(default_factory=list)
    resource_ids: list = field(default_factory=list)
    route_nodes: list = field(default_factory=list)
    through_tracks: list = field(default_factory=list)
    not_before: datetime | None = None

    def as_dict(self):
        return {"kind": self.kind, "kind_label": KIND_LABEL.get(self.kind, self.kind), "seq": self.seq,
                "track_id": self.track_id, "from_track_id": self.from_track_id, "side": self.side,
                "start": self.start.isoformat(), "end": self.end.isoformat(), "duration_min": self.duration_min,
                "requirements": self.requirements, "resource_ids": self.resource_ids,
                "route_nodes": self.route_nodes}


@dataclass
class PlaceResult:
    ok: bool
    ops: list[PlacedOp] = field(default_factory=list)
    track_reasons: dict = field(default_factory=dict)   # путь -> {code, message}
    failure_codes: set = field(default_factory=set)
    insufficient: list = field(default_factory=list)


class Placer:
    def __init__(self, model: StationModel, book: IntervalBook, *, wait_max_min: int = 90,
                 ignore_train: str | None = None):
        self.m = model
        self.book = book
        self.wait_max = timedelta(minutes=wait_max_min)
        self.ignore_train = ignore_train
        self.margin = model.cfg["processing"].get("length_margin_m", 10)
        self.buf = timedelta(minutes=model.cfg["processing"].get("track_buffer_min", 5))
        self.force_first: str | None = None  # путь первой стоянки задан (поезд уже на нём)

    # ------------------------------------------------------------ пути-кандидаты
    def candidates(self, group: str, spec: TrainSpec, steps: list[dict], res: PlaceResult) -> list[str]:
        out = []
        for tid, t in self.m.track_rows.items():
            if t.kind != group:
                continue
            label = self.m.track_label(tid)
            if spec.kind not in (t.allowed_train_kinds or []):
                res.track_reasons.setdefault(tid, {"code": "INCOMPATIBLE",
                                                   "message": f"{label}: не предназначен для поездов этого вида"})
                continue
            if t.zone_id:
                zone_ops = next((z.get("operations", []) for z in self.m.cfg["zones"] if z["id"] == t.zone_id), [])
                need = [s["kind"] for s in steps if s["kind"] in ("loading", "unloading", "repair")]
                if any(k not in zone_ops for k in need):
                    res.track_reasons.setdefault(tid, {"code": "INCOMPATIBLE",
                                                       "message": f"{label}: грузовой фронт не выполняет нужную операцию"})
                    continue
            if t.useful_length_m is None:
                res.track_reasons.setdefault(tid, {"code": "INSUFFICIENT_DATA",
                                                   "message": f"{label}: не задана полезная длина — недостаточно данных"})
                res.insufficient.append(f"полезная длина: {label[:1].lower() + label[1:]}")
                continue
            if spec.length_m is None:
                res.track_reasons.setdefault(tid, {"code": "INSUFFICIENT_DATA",
                                                   "message": f"{label}: длина состава неизвестна — недостаточно данных"})
                continue
            need_len = spec.length_m + self.margin
            if t.useful_length_m < need_len:
                res.track_reasons.setdefault(tid, {"code": "LENGTH",
                                                   "message": f"{label}: полезная длина {t.useful_length_m:.0f} м меньше "
                                                              f"требуемой {need_len:.1f} м (состав {spec.length_m:.1f} м + запас {self.margin} м)"})
                continue
            out.append(tid)
        out.sort(key=lambda tid: (self.m.track_rows[tid].useful_length_m, _num(self.m.track_rows[tid].number)))
        return out

    # ------------------------------------------------------------ проверки интервалов
    def _blocking(self, key: str, s: datetime, e: datetime) -> list[Entry]:
        return self.book.conflicts(key, s, e, ignore_train=self.ignore_train)

    def _choose_resources(self, reqs: list[str], kind: str, track_id: str, s: datetime, e: datetime,
                          used: set) -> tuple[list[str] | None, list[Entry], str | None]:
        chosen, blocks = [], []
        for rk in reqs:
            cands = sorted(r for r, row in self.m.resources.items() if row.kind == rk and r not in used)
            if not cands:
                return None, [], f"на станции нет ресурса «{RES_KIND_LABEL.get(rk, rk)}»"
            ok = None
            for rid in cands:
                row = self.m.resources[rid]
                pad = timedelta(minutes=self.m.zone_travel(row.home_zone_id, self.m.op_zone(kind, track_id)))
                b = self._blocking(f"res:{rid}", s - pad, e)
                if not b:
                    ok = rid
                    break
                blocks.extend(b)
            if ok is None:
                return None, blocks, f"нет свободного ресурса «{RES_KIND_LABEL.get(rk, rk)}»"
            chosen.append(ok)
            used.add(ok)
        return chosen, [], None

    def _route_for(self, kind: str, spec: TrainSpec, from_track: str | None, to_track: str | None):
        topo = self.m.topo
        if kind == "arrival":
            return topo.arrival_route(spec.side_in, to_track)
        if kind == "departure":
            return topo.departure_route(spec.side_out, from_track)
        if kind in ("shunting", "uncoupling"):
            return topo.shunting_route(from_track, to_track)
        return None

    def try_op(self, step: dict, spec: TrainSpec, track_id: str, from_track: str | None, earliest: datetime,
               exact: bool, seq: int) -> tuple[PlacedOp | None, str | None, list[Entry]]:
        """Самое раннее допустимое начало операции (или ровно earliest при exact)."""
        dur = timedelta(minutes=int(step["duration"]))
        kind = step["kind"]
        reqs = list(step.get("requires", []))
        route = None
        if kind in MOVEMENT_KINDS:
            route = self._route_for(kind, spec, from_track if kind != "arrival" else None,
                                    track_id if kind != "departure" else None)
            if route is None:
                return None, "ROUTE", []
        s = ceil_to(earliest, 1)
        limit = s + (timedelta(0) if exact else self.wait_max)
        last_blocks: list[Entry] = []
        last_code = None
        self.last_why = None
        while s <= limit:
            e = s + dur
            blocks: list[Entry] = []
            code = None
            if route is not None:
                for sw in route.switch_ids:
                    blocks += self._blocking(f"switch:{sw}", s, e)
                for tt in route.through_track_ids:
                    if tt != track_id and tt != from_track:
                        blocks += self._blocking(f"track:{tt}", s, e)
                if blocks:
                    code = "ROUTE"
                if not blocks and kind in ("arrival", "shunting", "uncoupling"):
                    # заезд возможен только на путь, свободный в момент заезда
                    blocks = self._blocking(f"track:{track_id}", s, e + self.buf)
                    if blocks:
                        code = "OCCUPIED"
            res_ids = []
            if not blocks and reqs:
                chosen, rb, why = self._choose_resources(reqs, kind, track_id, s, e, set())
                if chosen is None:
                    blocks = rb
                    code = "RESOURCE"
                    self.last_why = why
                    if not rb:  # ресурса нет вовсе — ждать бессмысленно
                        return None, "RESOURCE_NONE", []
                else:
                    res_ids = chosen
            if not blocks:
                op = PlacedOp(kind=kind, seq=seq, track_id=track_id, start=s, end=e, duration_min=int(step["duration"]),
                              from_track_id=from_track, requirements=reqs, resource_ids=res_ids,
                              side=spec.side_in if kind == "arrival" else (spec.side_out if kind == "departure" else None),
                              route_nodes=route.switch_ids if route else [],
                              through_tracks=route.through_track_ids if route else [])
                return op, None, []
            last_blocks, last_code = blocks, code
            nxt = min(b.end for b in blocks)
            if nxt >= FAR:
                break
            s = ceil_to(max(nxt, s + MIN), 1)
        return None, last_code, last_blocks

    # ------------------------------------------------------------ размещение цепочки
    def place(self, spec: TrainSpec, *, arrival_exact: bool = True) -> PlaceResult:
        res = PlaceResult(ok=False)
        if spec.length_m is None:
            res.insufficient.append("длина состава (нет длины вагонов и общей длины)")
        stays: list[tuple[str, list[dict]]] = []
        for st in spec.template:
            if st.get("group"):
                stays.append((st["group"], [st]))
            else:
                stays[-1][1].append(st)
        best: list[PlacedOp] | None = None

        def finish_stay(track: str, stay_start: datetime, stay_end: datetime, standing: bool = False) -> list[Entry]:
            b = self._blocking(f"track:{track}", stay_start, stay_end + self.buf)
            if standing:  # поезд уже стоит на пути: закрытие запрещает новые заезды, но не его стоянку
                b = [e for e in b if e.meta.get("type") not in ("closure", "maintenance", "data")]
            return b

        def dfs(i: int, prev_track: str | None, t_ready: datetime, acc: list[PlacedOp], seq: int,
                prev_stay_start: datetime | None, top_track: str | None) -> bool:
            nonlocal best
            group, steps = stays[i]
            cands = [self.force_first] if (i == 0 and self.force_first) else self.candidates(group, spec, steps, res)
            for track in cands:
                tt = top_track or track
                first = steps[0]
                op, code, blocks = self.try_op(first, spec, track, prev_track, t_ready,
                                               exact=(first["kind"] == "arrival" and arrival_exact), seq=seq)
                if op is None:
                    self._note(res, tt, track, code, blocks, first)
                    continue
                # предыдущая стоянка занята до окончания вытягивания
                if prev_track is not None:
                    b = finish_stay(prev_track, prev_stay_start, op.end, standing=(i == 1 and self.force_first is not None))
                    if b:
                        self._note(res, tt, prev_track, "OCCUPIED", b, first)
                        continue
                ops = [op]
                t = op.end
                failed = False
                k = 1
                while k < len(steps):
                    st = steps[k]
                    if st["kind"] == "departure":
                        earliest = max(t, spec.departure_not_before or t)
                    else:
                        earliest = t
                    if st["kind"] == "uncoupling":  # вагон подаётся с пути стоянки на путь депо
                        nop, code, blocks = self.try_op(st, spec, st["to_track"], track, earliest, exact=False, seq=seq + k)
                    else:
                        nop, code, blocks = self.try_op(st, spec, track, track if st["kind"] == "departure" else None,
                                                        earliest, exact=False, seq=seq + k)
                    if nop is None:
                        self._note(res, tt, track, code, blocks, st)
                        failed = True
                        break
                    ops.append(nop)
                    t = nop.end
                    k += 1
                if failed:
                    continue
                stay_start = op.start
                if i + 1 < len(stays):
                    if dfs(i + 1, track, t, acc + ops, seq + len(steps), stay_start, tt):
                        return True
                    continue
                b = finish_stay(track, stay_start, t, standing=(i == 0 and self.force_first is not None))
                if b:
                    self._note(res, tt, track, "OCCUPIED", b, steps[-1])
                    continue
                best = acc + ops
                return True
            return False

        if stays and dfs(0, None, spec.arrival, [], 1, None, None):
            res.ok = True
            res.ops = best
        return res

    def _note(self, res: PlaceResult, top_track: str, track: str, code: str | None, blocks: list[Entry], step: dict):
        label = self.m.track_label(track)
        what = KIND_LABEL.get(step["kind"], step["kind"]).lower()
        if code == "ROUTE" and not blocks:
            msg = f"{label}: нет маршрута по топологии для операции «{what}»"
        elif code == "RESOURCE_NONE":
            msg = f"для операции «{what}» на станции нет нужного ресурса"
        elif blocks:
            b = blocks[0]
            who = describe_block(b)
            if b.meta.get("type") == "data":
                code = "DATA"
                msg = who
            elif b.meta.get("type") in ("closure", "maintenance"):
                code = "CLOSED"
                msg = f"{label}: {who}"
            elif code == "RESOURCE":
                busy_until = min(x.end for x in blocks)
                msg = (f"{label}: {self.last_why or 'нет свободного ресурса'} для операции «{what}» в пределах "
                       f"допустимого ожидания (ближайшее освобождение — {local_hm(busy_until)})")
            elif code == "ROUTE":
                msg = f"маршрут для операции «{what}» на {label[:1].lower() + label[1:]} занят: {who}"
            else:
                code = "OCCUPIED"
                msg = f"{label}: {who}"
        else:
            msg = f"{label}: размещение невозможно"
        res.failure_codes.add(code or "UNKNOWN")
        res.track_reasons.setdefault(top_track, {"code": code, "message": msg})


def _num(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return 0


def commit_to_book(model: StationModel, book: IntervalBook, train_id: str, number: str, ops: list[PlacedOp]):
    """Добавить размещённые операции в книгу (для последовательного размещения нескольких поездов)."""
    from app.services.model import reservation_specs
    dicts = [{"id": f"tmp-{train_id}-{o.seq}", "kind": o.kind, "track_id": o.track_id,
              "from_track_id": o.from_track_id, "side": o.side, "start": o.start, "end": o.end, "resource_ids": o.resource_ids,
              "route_nodes": o.route_nodes, "status": "planned"} for o in ops]
    for sp in reservation_specs(model, train_id, number, dicts):
        book.add(sp["key"], sp["start"], sp["end"], type="reservation", train_id=train_id,
                 op_id=sp["operation_id"], label=sp["purpose"])
