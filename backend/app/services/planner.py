"""Перепланирование: OR-Tools CP-SAT + эвристика + независимая проверка результата.

Модель (минуты от текущего модельного времени):
  * переменные: начало каждой не начатой операции; выбор пути для каждой стоянки;
    выбор конкретного ресурса для каждого требования операции;
  * жёсткие ограничения: технологическая последовательность (s[k+1] ≥ s[k] + d[k]);
    «не ранее» (прибытие к станции, расписание отправления); на пути — не более одной
    стоянки одновременно (с запасом), закрытия/окна обслуживания/недостоверные данные;
    стрелки маршрута — не более одного движения одновременно (маршрут зависит от выбранных
    путей); ресурс — не более одной операции одновременно с учётом времени перехода между
    зонами, только в смене, не в неисправности; отправление не в ограничение соседа;
  * цель: минимизация взвешенной задержки (вес 2^(приоритет-1)) + штраф за изменение пути
    (согласованная операция — 30, плановая — 8) + штраф за сдвиг времени (согласованная —
    1 за мин, плановая — 0,2 за мин). Задержка учитывается с весом ×10.

Жёсткие ограничения никогда не ослабляются ради цели. Результат дополнительно проверяется
независимым валидатором (книга интервалов) — план, не прошедший проверку, не предлагается.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ortools.sat.python import cp_model

from app.config import get_settings
from app.core import metrics
from app.core.timeutil import aware, iso, local_hm, utcnow
from app.services.model import FAR, KIND_LABEL, MOVEMENT_KINDS, IntervalBook, StationModel, reservation_specs, stays_of, with_presence
from app.services.placement import Placer, TrainSpec, commit_to_book

log = logging.getLogger("planner")

H_MAX = 24 * 60
STATUS = {
    "optimal": "Оптимальность доказана",
    "feasible": "Найден допустимый план (оптимальность не доказана за отведённое время)",
    "timeout": "Допустимый план не найден за отведённое время",
    "infeasible": "Доказано отсутствие решения",
    "heuristic": "Эвристика: найден допустимый план (оптимальность не оценивалась)",
    "partial": "Полный допустимый план не найден: сохранены допустимые части прежнего плана",
}
ASSUMPTIONS = [
    "Длительности операций — нормативы модели; задержки от инцидентов добавлены к длительности.",
    "Операции, которые уже выполняются, не переносятся; поезд на пути остаётся на нём до выхода.",
    "Маршрут — кратчайший путь по топологии; одновременно через стрелку проходит одно движение.",
    "Время перехода ресурса между зонами учитывается как подготовка перед операцией.",
    "Поезд не может прибыть раньше прогнозного времени и отправиться раньше расписания.",
    "Горизонт планирования — 24 ч; приоритеты поездов — веса 2^(приоритет−1).",
]


@dataclass
class OpInfo:
    op: object
    idx: int
    fixed: bool
    lb: int
    dur: int
    start0: int        # текущее плановое начало (для штрафа сдвига)
    confirmed: bool
    stay: int | None = None


@dataclass
class PlanResult:
    status: str
    solver: str
    solve_ms: float
    schedule: dict = field(default_factory=dict)       # op_id -> {start, end, track_id, from_track_id, resource_ids, route_nodes}
    objective: float | None = None
    unresolved: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def _m(model: StationModel, dt: datetime) -> int:
    return int((aware(dt) - model.t_base).total_seconds() // 60)


def _dt(model: StationModel, m: int) -> datetime:
    return model.t_base + timedelta(minutes=m)


class PlanBuilder:
    def __init__(self, model: StationModel, fc: dict):
        self.m = model
        model.t_base = model.now.replace(second=0, microsecond=0)
        self.fc = fc
        self.buf = model.cfg["processing"].get("track_buffer_min", 5)
        self.margin = model.cfg["processing"].get("length_margin_m", 10)
        self.chains: list[list[OpInfo]] = []
        self.stays: list[dict] = []
        self.unresolved: list[dict] = []
        self._collect()

    def _collect(self):
        m = self.m
        for tid, ops in m.ops_by_train.items():
            t = m.trains[tid]
            if t.status in ("departed", "completed", "cancelled"):
                continue
            live = [o for o in ops if o.status not in ("done", "cancelled")]
            if not live:
                continue
            self.chains.append(self._chain(live, t))
        for o in m.ops.values():
            if not o.train_id and o.status not in ("done", "cancelled"):
                self.chains.append(self._chain([o], None))

    def _chain(self, live, train) -> list[OpInfo]:
        m = self.m
        chain = []
        for i, o in enumerate(live):
            fixed = o.status == "in_progress"
            dur = o.duration_min + (o.extra_delay_min or 0)
            fs = self.fc.get(o.id, (aware(o.planned_start), aware(o.planned_end)))[0]
            lb = 0
            if o.not_before:
                lb = max(lb, _m(m, o.not_before))
            if o.kind == "arrival" and train and train.expected_arrival:
                lb = max(lb, _m(m, train.expected_arrival))
            if fixed:
                lb = _m(m, o.actual_start or o.planned_start)
            chain.append(OpInfo(o, i, fixed, lb, dur, _m(m, o.planned_start) if o.reserved else _m(m, fs),
                                o.status == "confirmed"))
        # стоянки
        dicts = [{"id": ci.op.id, "kind": ci.op.kind, "track_id": ci.op.track_id, "from_track_id": ci.op.from_track_id,
                  "start": 0, "end": 0} for ci in chain]
        sts = stays_of(dicts)
        for k, st in enumerate(sts):
            sidx = len(self.stays)
            ids = st["ops"]
            infos = [ci for ci in chain if ci.op.id in ids]
            first = infos[0]
            # стоянка с начатой операцией или поезд уже на пути — путь фиксирован
            standing = train is not None and k == 0 and train.status == "on_station" and train.current_track_id == st["track_id"]
            fixed_track = standing or any(ci.fixed for ci in infos)
            cands = [st["track_id"]] if fixed_track else self._candidates(st["track_id"], train, infos)
            unresolved = False
            if not cands:
                cands = [st["track_id"]]
                unresolved = True
                self.unresolved.append({"track_id": st["track_id"], "train": train.number if train else None,
                                        "reason": "нет допустимого пути для стоянки (все совместимые пути закрыты, "
                                                  "без данных или недостаточной длины)"})
            self.stays.append({"idx": sidx, "ops": infos, "track0": st["track_id"], "cands": cands,
                               "fixed": fixed_track, "unresolved": unresolved, "train": train,
                               "confirmed": any(ci.confirmed for ci in infos)})
            for ci in infos:
                # манёвр закрывает предыдущую стоянку и открывает новую: относится к пути назначения
                if ci.stay is None or ci is infos[0]:
                    ci.stay = sidx
        return chain

    def _candidates(self, track0: str, train, infos) -> list[str]:
        m = self.m
        row0 = m.track_rows[track0]
        L = m.train_length(train) if train else None
        need_ops = {ci.op.kind for ci in infos} & {"loading", "unloading", "repair"}
        out = []
        for tid, t in m.track_rows.items():
            if t.kind != row0.kind:
                continue
            if train and train.kind not in (t.allowed_train_kinds or []):
                continue
            if L is not None and t.useful_length_m is not None and t.useful_length_m < L + self.margin:
                continue
            if t.useful_length_m is None:
                continue
            if t.zone_id and need_ops:
                zops = next((z.get("operations", []) for z in m.cfg["zones"] if z["id"] == t.zone_id), [])
                if any(k not in zops for k in need_ops):
                    continue
            if need_ops and not t.zone_id:
                continue
            ds = m.data_states.get(tid)
            if ds and ds["state"] not in ("actual", "not_monitored"):
                continue
            out.append(tid)
        return out

    # ------------------------------------------------------------------ CP-SAT
    def solve_cpsat(self, time_limit: float, workers: int) -> PlanResult:
        m = self.m
        mdl = cp_model.CpModel()
        S, E, IV = {}, {}, {}
        for chain in self.chains:
            prev = None
            for ci in chain:
                o = ci.op
                if ci.fixed:
                    s = mdl.NewConstant(ci.lb)
                    e = mdl.NewConstant(max(ci.lb + ci.dur, 0))
                else:
                    lb = max(ci.lb, 0)
                    s = mdl.NewIntVar(lb, H_MAX, f"s_{o.id}")
                    e = mdl.NewIntVar(lb + ci.dur, H_MAX + ci.dur, f"e_{o.id}")
                    mdl.Add(e == s + ci.dur)
                S[o.id], E[o.id] = s, e
                if prev is not None and not ci.fixed:
                    mdl.Add(s >= E[prev.op.id])
                prev = ci
        # выбор путей стоянок
        X = {}
        track_iv: dict[str, list] = {}
        track_iv_free: dict[str, list] = {}
        for st in self.stays:
            first, last = st["ops"][0], st["ops"][-1]
            # стоянка заканчивается с окончанием выхода с пути (следующий манёвр/отправление уже включены)
            st_s, st_e = S[first.op.id], E[last.op.id]
            if st["fixed"] and not first.fixed:
                st_s = mdl.NewConstant(0)  # поезд уже стоит на пути: путь занят с текущего момента
            size = mdl.NewIntVar(0, 2 * H_MAX, f"sz_{st['idx']}")
            end_b = mdl.NewIntVar(-H_MAX, 3 * H_MAX, f"eb_{st['idx']}")
            mdl.Add(end_b == st_e + self.buf)
            mdl.Add(size == end_b - st_s)
            lits = []
            for t in st["cands"]:
                x = mdl.NewBoolVar(f"x_{st['idx']}_{t}")
                X[(st["idx"], t)] = x
                lits.append(x)
                iv = mdl.NewOptionalIntervalVar(st_s, size, end_b, x, f"iv_{st['idx']}_{t}")
                track_iv.setdefault(t, []).append(iv)
                if not st["fixed"] and not st["unresolved"]:
                    track_iv_free.setdefault(t, []).append(iv)
            mdl.AddExactlyOne(lits)
        # блокировки путей
        blocks = IntervalBook()
        m.add_blocks(blocks, include_data_blocks=False)
        for t in m.track_rows:
            for en in blocks.data.get(f"track:{t}", []):
                a, b = max(_m(m, en.start), -H_MAX), min(_m(m, en.end) if en.end < FAR else 3 * H_MAX, 3 * H_MAX)
                if b > a and b > 0:
                    track_iv_free.setdefault(t, []).append(mdl.NewIntervalVar(a, b - a, b, f"blk_{t}_{a}"))
        # отцепка вагона занимает и путь депо (вагон подаётся туда); ремонт — только после отцепки
        unc_by_wagon = {}
        for chain in self.chains:
            for ci in chain:
                o = ci.op
                if o.kind == "uncoupling" and o.track_id:
                    iv = mdl.NewIntervalVar(S[o.id], ci.dur + self.buf, mdl.NewIntVar(-H_MAX, 3 * H_MAX, f"ue_{o.id}"),
                                            f"unc_{o.id}")
                    mdl.Add(iv.EndExpr() == E[o.id] + self.buf)
                    track_iv.setdefault(o.track_id, []).append(iv)
                    if not ci.fixed:
                        track_iv_free.setdefault(o.track_id, []).append(iv)
                    num = (o.note or "").split("№")[-1].strip()
                    unc_by_wagon[num] = o.id
        for chain in self.chains:
            for ci in chain:
                o = ci.op
                if o.kind == "repair" and not ci.fixed:
                    num = (o.note or "").split("№")[-1].strip()
                    if num in unc_by_wagon:
                        mdl.Add(S[o.id] >= E[unc_by_wagon[num]])
        for t, ivs in track_iv.items():
            mdl.AddNoOverlap(ivs)
        for t, ivs in track_iv_free.items():
            mdl.AddNoOverlap(ivs)
        # маршруты: стрелки
        stay_of_op = {ci.op.id: ci.stay for ch in self.chains for ci in ch}
        sw_iv: dict[str, list] = {}
        sw_iv_free: dict[str, list] = {}
        topo = m.topo
        self.route_choice = {}
        for chain in self.chains:
            for ci in chain:
                o = ci.op
                if o.kind not in MOVEMENT_KINDS:
                    continue
                if ci.fixed:
                    for sw in o.route_nodes or []:
                        sw_iv.setdefault(sw, []).append(mdl.NewIntervalVar(S[o.id], ci.dur, E[o.id], f"fsw_{o.id}_{sw}"))
                    continue
                cur = self.stays[ci.stay]
                options = []  # (presence literal, route, to_track, from_track)
                if o.kind == "arrival":
                    for t in cur["cands"]:
                        r = topo.arrival_route(o.side or "west", t)
                        if r:
                            options.append((X[(cur["idx"], t)], r, t, None))
                elif o.kind == "departure":
                    for t in cur["cands"]:
                        r = topo.departure_route(o.side or "east", t)
                        if r:
                            options.append((X[(cur["idx"], t)], r, t, None))
                elif o.kind == "uncoupling":
                    for t in cur["cands"]:
                        r = topo.shunting_route(t, o.track_id)
                        if r:
                            options.append((X[(cur["idx"], t)], r, o.track_id, t))
                else:  # shunting: из предыдущей стоянки в текущую
                    prev_idx = ci.stay - 1 if ci.stay > 0 and self.stays[ci.stay - 1]["train"] is cur["train"] else None
                    prev_cands = self.stays[prev_idx]["cands"] if prev_idx is not None else [o.from_track_id]
                    for f in prev_cands:
                        for t in cur["cands"]:
                            r = topo.shunting_route(f, t)
                            if not r:
                                continue
                            b = mdl.NewBoolVar(f"r_{o.id}_{f}_{t}")
                            xt = X[(cur["idx"], t)]
                            if prev_idx is not None:
                                xf = X[(prev_idx, f)]
                                mdl.AddBoolAnd([xf, xt]).OnlyEnforceIf(b)
                                mdl.AddBoolOr([xf.Not(), xt.Not()]).OnlyEnforceIf(b.Not())
                            else:
                                mdl.Add(b == xt)
                            options.append((b, r, t, f))
                if not options:
                    self.unresolved.append({"train": cur["train"].number if cur["train"] else None,
                                            "reason": f"нет маршрута для операции «{KIND_LABEL.get(o.kind)}»"})
                    continue
                mdl.AddExactlyOne([p for p, *_ in options])
                self.route_choice[o.id] = options
                for p, r, t, f in options:
                    for sw in r.switch_ids:
                        iv = mdl.NewOptionalIntervalVar(S[o.id], ci.dur, E[o.id], p, f"sw_{o.id}_{sw}_{t}_{f}")
                        sw_iv.setdefault(sw, []).append(iv)
                        sw_iv_free.setdefault(sw, []).append(iv)
        for sw, lst in sw_iv.items():
            mdl.AddNoOverlap(lst)
        for sw in list(sw_iv_free):
            for en in blocks.data.get(f"switch:{sw}", []):
                a, b = max(_m(m, en.start), -H_MAX), min(_m(m, en.end) if en.end < FAR else 3 * H_MAX, 3 * H_MAX)
                if b > a and b > 0:
                    sw_iv_free[sw].append(mdl.NewIntervalVar(a, b - a, b, f"bsw_{sw}_{a}"))
            mdl.AddNoOverlap(sw_iv_free[sw])
        # ресурсы
        res_iv: dict[str, list] = {}
        res_iv_free: dict[str, list] = {}
        self.res_choice = {}
        for chain in self.chains:
            for ci in chain:
                o = ci.op
                if ci.fixed:
                    for rid in o.resource_ids or []:
                        res_iv.setdefault(rid, []).append(mdl.NewIntervalVar(S[o.id], ci.dur, E[o.id], f"fr_{o.id}_{rid}"))
                    continue
                zone = m.op_zone(o.kind, o.track_id)
                for k, rk in enumerate(o.requirements or []):
                    cands = [r for r, row in m.resources.items() if row.kind == rk]
                    if not cands:
                        self.unresolved.append({"train": None, "reason": f"на станции нет ресурса вида «{rk}»"})
                        continue
                    ys = []
                    for rid in cands:
                        y = mdl.NewBoolVar(f"y_{o.id}_{k}_{rid}")
                        ys.append((y, rid))
                        pad = m.zone_travel(m.resources[rid].home_zone_id, zone)
                        st_pad = mdl.NewIntVar(-H_MAX, H_MAX + 1, f"sp_{o.id}_{k}_{rid}")
                        mdl.Add(st_pad == S[o.id] - pad)
                        iv = mdl.NewOptionalIntervalVar(st_pad, ci.dur + pad, E[o.id], y, f"ri_{o.id}_{k}_{rid}")
                        res_iv.setdefault(rid, []).append(iv)
                        res_iv_free.setdefault(rid, []).append(iv)
                    mdl.AddExactlyOne([y for y, _ in ys])
                    self.res_choice[(o.id, k)] = ys
        for rid, lst in res_iv.items():
            mdl.AddNoOverlap(lst)
        for rid in m.resources:
            free = res_iv_free.get(rid, [])
            if not free:
                continue
            for en in blocks.data.get(f"res:{rid}", []):
                a, b = max(_m(m, en.start), -H_MAX), min(_m(m, en.end) if en.end < FAR else 3 * H_MAX, 3 * H_MAX)
                if b > a and b > -H_MAX:
                    free.append(mdl.NewIntervalVar(a, b - a, b, f"br_{rid}_{a}"))
            mdl.AddNoOverlap(free)
        # ограничения соседей для отправлений
        nb_iv: dict[str, list] = {}
        for chain in self.chains:
            for ci in chain:
                o = ci.op
                if o.kind == "departure" and not ci.fixed and o.train_id:
                    dest = m.trains[o.train_id].destination_station_id
                    if dest:
                        nb_iv.setdefault(dest, []).append(mdl.NewIntervalVar(S[o.id], ci.dur, E[o.id], f"nb_{o.id}"))
        for dest, lst in nb_iv.items():
            ens = blocks.data.get(f"neighbor:{dest}", [])
            if not ens:
                continue
            for en in ens:
                a, b = max(_m(m, en.start), 0), min(_m(m, en.end) if en.end < FAR else 3 * H_MAX, 3 * H_MAX)
                if b > a:
                    lst.append(mdl.NewIntervalVar(a, b - a, b, f"bn_{dest}_{a}"))
            mdl.AddNoOverlap(lst)
        # цель
        terms = []
        self.delay_vars = {}
        for chain in self.chains:
            if not chain or chain[0].op.train_id is None:
                continue
            train = m.trains[chain[0].op.train_id]
            w = 2 ** (max(1, min(5, train.priority)) - 1)
            dep = next((ci for ci in chain if ci.op.kind == "departure"), None)
            if dep and train.scheduled_departure and not dep.fixed:
                d = mdl.NewIntVar(0, 2 * H_MAX, f"d_{train.id}")
                mdl.Add(d >= S[dep.op.id] - _m(m, train.scheduled_departure))
                terms.append(10 * w * d)
                self.delay_vars[train.id] = d
            arr = next((ci for ci in chain if ci.op.kind == "arrival" and not ci.fixed), None)
            if arr:
                h = mdl.NewIntVar(0, 2 * H_MAX, f"h_{train.id}")
                mdl.Add(h >= S[arr.op.id] - max(arr.lb, 0))
                terms.append(10 * w * h)
            if not dep:  # цепочки без отправления (переработка): задержка окончания
                last = chain[-1]
                if not last.fixed:
                    d2 = mdl.NewIntVar(0, 2 * H_MAX, f"d2_{train.id}")
                    mdl.Add(d2 >= E[last.op.id] - (max(last.start0, 0) + last.dur))
                    terms.append(5 * w * d2)
        for st in self.stays:
            if st["fixed"]:
                continue
            for t in st["cands"]:
                if t != st["track0"]:
                    terms.append((30 if st["confirmed"] else 8) * 10 * X[(st["idx"], t)])
        for chain in self.chains:
            for ci in chain:
                if ci.fixed or not ci.op.reserved:
                    continue
                dev = mdl.NewIntVar(0, 3 * H_MAX, f"dv_{ci.op.id}")
                mdl.AddAbsEquality(dev, S[ci.op.id] - ci.start0)
                terms.append((10 if ci.confirmed else 2) * dev)
        mdl.Minimize(sum(terms) if terms else 0)
        # подсказка: текущий план
        for chain in self.chains:
            for ci in chain:
                if not ci.fixed:
                    mdl.AddHint(S[ci.op.id], max(ci.lb, ci.start0, 0))
        for st in self.stays:
            for t in st["cands"]:
                mdl.AddHint(X[(st["idx"], t)], 1 if t == st["track0"] else 0)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = time_limit
        solver.parameters.num_workers = workers
        solver.parameters.random_seed = 7
        t0 = time.perf_counter()
        st_code = solver.Solve(mdl)
        ms = (time.perf_counter() - t0) * 1000
        status = {cp_model.OPTIMAL: "optimal", cp_model.FEASIBLE: "feasible", cp_model.INFEASIBLE: "infeasible",
                  cp_model.MODEL_INVALID: "infeasible", cp_model.UNKNOWN: "timeout"}[st_code]
        res = PlanResult(status=status, solver="CP-SAT", solve_ms=round(ms, 1), unresolved=list(self.unresolved))
        if status in ("optimal", "feasible"):
            res.objective = solver.ObjectiveValue()
            for st in self.stays:
                chosen = next(t for t in st["cands"] if solver.Value(X[(st["idx"], t)]))
                st["chosen"] = chosen
            for chain in self.chains:
                for ci in chain:
                    o = ci.op
                    stay = self.stays[ci.stay]
                    s = solver.Value(S[o.id])
                    entry = {"start": _dt(m, s), "end": _dt(m, s + ci.dur), "track_id": o.track_id,
                             "from_track_id": o.from_track_id, "resource_ids": list(o.resource_ids or []),
                             "route_nodes": list(o.route_nodes or []), "fixed": ci.fixed}
                    if not ci.fixed:
                        if o.kind in ("arrival", "departure"):
                            entry["track_id"] = stay["chosen"]
                        elif o.kind == "shunting":
                            entry["track_id"] = stay["chosen"]
                        elif o.kind == "uncoupling":
                            entry["from_track_id"] = stay["chosen"]
                        elif o.kind not in MOVEMENT_KINDS:
                            entry["track_id"] = stay["chosen"]
                        if o.id in self.route_choice:
                            for p, r, t, f in self.route_choice[o.id]:
                                if solver.Value(p):
                                    entry["route_nodes"] = r.switch_ids
                                    if o.kind == "shunting":
                                        entry["from_track_id"] = f
                        rids = []
                        for k, _ in enumerate(o.requirements or []):
                            for y, rid in self.res_choice.get((o.id, k), []):
                                if solver.Value(y):
                                    rids.append(rid)
                        entry["resource_ids"] = rids
                    res.schedule[o.id] = entry
        return res

    # ------------------------------------------------------------------ эвристика
    def solve_greedy(self) -> PlanResult:
        """Последовательное размещение цепочек (earliest-fit): сначала стоящие на путях поезда,
        затем остальные по убыванию приоритета."""
        m = self.m
        t0 = time.perf_counter()
        book = IntervalBook()
        m.add_blocks(book, include_data_blocks=True)
        res = PlanResult(status="heuristic", solver="Эвристика earliest-fit", solve_ms=0, unresolved=list(self.unresolved))
        for chain in self.chains:
            for ci in chain:
                o = ci.op
                if ci.fixed:
                    entry = {"start": _dt(m, ci.lb), "end": _dt(m, ci.lb + ci.dur), "track_id": o.track_id,
                             "from_track_id": o.from_track_id, "resource_ids": list(o.resource_ids or []),
                             "route_nodes": list(o.route_nodes or []), "fixed": True}
                    res.schedule[o.id] = entry
                    for sp in reservation_specs(m, o.train_id, "", [{"id": o.id, "kind": o.kind, "track_id": o.track_id,
                                                                    "from_track_id": o.from_track_id, "start": entry["start"],
                                                                    "end": entry["end"], "resource_ids": o.resource_ids,
                                                                    "route_nodes": o.route_nodes, "side": o.side,
                                                                    "status": "in_progress"}]):
                        book.add(sp["key"], sp["start"], sp["end"], type="reservation", train_id=o.train_id)

        def key(ch):
            st = self.stays[ch[0].stay]
            pr = m.trains[ch[0].op.train_id].priority if ch[0].op.train_id else 0
            return (0 if st["fixed"] else 1, -pr, ch[0].lb)

        for chain in sorted(self.chains, key=key):
            rest = [ci for ci in chain if not ci.fixed]
            if not rest:
                continue
            o0 = rest[0].op
            train = m.trains.get(o0.train_id) if o0.train_id else None
            first_stay = self.stays[rest[0].stay]
            standing = first_stay["fixed"]
            steps = []
            if standing:
                steps.append({"kind": "dwell", "duration": 0, "requires": [],
                              "group": m.track_rows[first_stay["track0"]].kind})
            for ci in rest:
                st = {"kind": ci.op.kind, "duration": ci.dur, "requires": list(ci.op.requirements or [])}
                if ci.op.kind in ("arrival", "shunting") or (not steps):
                    st["group"] = m.track_rows[ci.op.track_id].kind
                if ci.op.kind == "uncoupling":
                    st["to_track"] = ci.op.track_id
                steps.append(st)
            start_after = max(0, rest[0].lb)
            prev_end = max((ci.lb + ci.dur for ci in chain if ci.fixed), default=None)
            if prev_end is not None:
                start_after = max(start_after, prev_end)
            if standing:
                start_after = max(0, prev_end or 0)
            spec = TrainSpec(train_id=train.id if train else o0.id, number=train.number if train else (o0.note or ""),
                             kind=train.kind if train else "freight", priority=train.priority if train else 1,
                             length_m=m.train_length(train) if train else 20.0,
                             side_in=train.arrival_side if train else "east",
                             side_out=train.departure_side if train else "east",
                             template=steps, arrival=_dt(m, start_after),
                             departure_not_before=aware(train.scheduled_departure) if train and train.scheduled_departure else None)
            placer = Placer(m, book, wait_max_min=600)
            if standing or rest[0].op.kind not in ("arrival", "shunting"):
                placer.force_first = first_stay["track0"]
            pr = placer.place(spec, arrival_exact=False)
            if not pr.ok:
                res.unresolved.append({"train": spec.number, "reason": "эвристика не нашла размещения в горизонте"})
                for ci in rest:
                    o = ci.op
                    res.schedule[o.id] = {"start": aware(o.planned_start), "end": aware(o.planned_end),
                                          "track_id": o.track_id, "from_track_id": o.from_track_id,
                                          "resource_ids": list(o.resource_ids or []),
                                          "route_nodes": list(o.route_nodes or []), "fixed": False, "kept": True}
                continue
            commit_to_book(m, book, spec.train_id, spec.number, pr.ops)
            placed = pr.ops[1:] if standing else pr.ops
            for ci, po in zip(rest, placed):
                res.schedule[ci.op.id] = {"start": po.start, "end": po.end, "track_id": po.track_id,
                                          "from_track_id": po.from_track_id or ci.op.from_track_id,
                                          "resource_ids": po.resource_ids, "route_nodes": po.route_nodes, "fixed": False}
        res.solve_ms = round((time.perf_counter() - t0) * 1000, 1)
        if res.unresolved:
            res.status = "partial"
        return res


# ---------------------------------------------------------------------- проверка и сводка
def verify(model: StationModel, schedule: dict) -> list[str]:
    """Независимая проверка плана: все резервы плана не пересекаются друг с другом и с блокировками."""
    book = IntervalBook()
    model.add_blocks(book, include_data_blocks=True)
    errors = []
    by_train: dict = {}
    for oid, e in schedule.items():
        o = model.ops[oid]
        by_train.setdefault(o.train_id or oid, []).append((o, e))
    for key, items in by_train.items():
        items.sort(key=lambda p: p[0].seq)
        train = model.trains.get(items[0][0].train_id) if items[0][0].train_id else None
        dicts = [{"id": o.id, "kind": o.kind, "track_id": e["track_id"], "from_track_id": e.get("from_track_id"),
                  "start": e["start"], "end": e["end"], "resource_ids": e["resource_ids"], "route_nodes": e["route_nodes"],
                  "side": o.side, "status": "in_progress" if e.get("fixed") else "planned"} for o, e in items]
        dicts = with_presence(model, train, dicts)
        fixed_ops = {o.id for o, e in items if e.get("fixed")} | {d["id"] for d in dicts if d["id"].startswith("presence-")}
        prev_end = None
        for o, e in items:
            if prev_end and e["start"] < prev_end and not e.get("fixed"):
                errors.append(f"нарушена последовательность операций поезда № {train.number if train else key}")
            prev_end = e["end"]
        for sp in reservation_specs(model, items[0][0].train_id, train.number if train else "", dicts):
            fixed = sp["operation_id"] in fixed_ops
            hits = book.conflicts(sp["key"], sp["start"], sp["end"])
            for h in hits:
                if h.meta.get("type") == "reservation" or not fixed:
                    if h.meta.get("type") == "data" and fixed:
                        continue
                    if h.meta.get("type") == "shift" and fixed:
                        continue
                    errors.append(f"{sp['key']}: {sp['purpose']} {local_hm(sp['start'])}–{local_hm(sp['end'])} "
                                  f"пересекается с «{h.meta.get('label', h.meta.get('type'))}»")
                    break
            book.add(sp["key"], sp["start"], sp["end"], type="reservation", label=sp["purpose"], train_id=sp["train_id"])
    return errors


def run_planner(model: StationModel, *, time_limit: float | None = None, trigger: str = "manual") -> dict:
    from app.services.conflicts import detect
    from app.services.index import compute_plan_index
    s = get_settings()
    t_all = time.perf_counter()
    from app.services.forecast import forecast
    fc_before = forecast(model)
    before = detect(model, fc_before)
    pb = PlanBuilder(model, fc_before)
    res = pb.solve_cpsat(time_limit or s.planner_time_limit_s, s.planner_workers)
    notes = []
    if res.status in ("optimal", "feasible"):
        errs = verify(model, res.schedule)
        if errs:
            notes.append("Решение CP-SAT не прошло независимую проверку и отклонено: " + "; ".join(errs[:3]))
            log.warning("CP-SAT verify failed: %s", errs[:5])
            res = None
    else:
        notes.append(f"CP-SAT: {STATUS[res.status]} ({res.solve_ms:.0f} мс).")
        res_cp = res
        res = None
    if res is None:
        pb2 = PlanBuilder(model, fc_before)
        res = pb2.solve_greedy()
        errs = verify(model, {k: v for k, v in res.schedule.items() if not v.get("kept")})
        if errs:
            notes.append("Эвристика: часть плана не прошла проверку — " + "; ".join(errs[:3]))
            res.status = "partial"
    total_ms = (time.perf_counter() - t_all) * 1000
    metrics.observe("plan_compute_ms", total_ms)
    changes = diff_schedule(model, res.schedule)
    kpi_before = plan_kpis(model, {oid: {"start": fc_before[oid][0], "end": fc_before[oid][1]} for oid in fc_before
                                   if oid in model.ops and model.ops[oid].status not in ("done", "cancelled")}, before["conflicts"])
    kpi_after = plan_kpis(model, res.schedule, [])
    remaining = [c for c in before["conflicts"] if c["type"] in ("faulty_wagon",)]
    kpi_after["conflicts"] = len(remaining) + len(res.unresolved)
    idx_before = compute_plan_index(model, None, kpi_before)
    idx_after = compute_plan_index(model, res.schedule, kpi_after)
    return {
        "status": res.status, "status_label": STATUS[res.status], "solver": res.solver,
        "solve_ms": res.solve_ms, "total_ms": round(total_ms, 1), "objective": res.objective,
        "schedule": res.schedule, "changes": changes, "unresolved": res.unresolved, "notes": notes,
        "summary": {"before": kpi_before, "after": kpi_after, "index_before": idx_before, "index_after": idx_after,
                    "conflicts_before": len(before["conflicts"]), "trigger": trigger,
                    "objective_text": "Минимизация взвешенной задержки (вес 2^(приоритет−1)) и штрафов за изменение "
                                      "согласованных операций; жёсткие ограничения не ослабляются."},
        "assumptions": ASSUMPTIONS,
    }


def diff_schedule(model: StationModel, schedule: dict) -> list[dict]:
    out = []
    for oid, e in schedule.items():
        if e.get("fixed"):
            continue
        o = model.ops[oid]
        moved = int(round((e["start"] - aware(o.planned_start)).total_seconds() / 60))
        track_changed = e["track_id"] != o.track_id or (e.get("from_track_id") or None) != (o.from_track_id or None)
        res_changed = sorted(e["resource_ids"] or []) != sorted(o.resource_ids or [])
        if moved == 0 and not track_changed and not res_changed and o.reserved:
            continue
        t = model.trains.get(o.train_id) if o.train_id else None
        out.append({"operation_id": oid, "train": t.number if t else None, "kind": o.kind,
                    "kind_label": KIND_LABEL.get(o.kind, o.kind), "status": o.status,
                    "from": {"track": model.track_label(o.track_id), "start": iso(o.planned_start),
                             "resources": [model.resources[r].name for r in (o.resource_ids or []) if r in model.resources]},
                    "to": {"track": model.track_label(e["track_id"]), "start": iso(e["start"]),
                           "resources": [model.resources[r].name for r in (e["resource_ids"] or []) if r in model.resources]},
                    "shift_min": moved, "track_changed": track_changed, "resources_changed": res_changed,
                    "newly_planned": not o.reserved, "was_confirmed": o.status == "confirmed"})
    out.sort(key=lambda c: (c["train"] or "", c["to"]["start"]))
    return out


def plan_kpis(model: StationModel, sched: dict, conflicts: list) -> dict:
    delays = []
    weighted = 0.0
    for tid, ops in model.ops_by_train.items():
        t = model.trains[tid]
        dep = next((o for o in ops if o.kind == "departure" and o.id in sched), None)
        if dep and t.scheduled_departure:
            d = max(0.0, (sched[dep.id]["start"] - aware(t.scheduled_departure)).total_seconds() / 60)
            delays.append(d)
            weighted += d * 2 ** (max(1, min(5, t.priority)) - 1)
    busy = {}
    h0, h1 = model.now, model.now + timedelta(hours=4)
    for oid, e in sched.items():
        o = model.ops.get(oid)
        if not o:
            continue
        tr = e.get("track_id")
        if tr and model.track_rows.get(tr) and model.track_rows[tr].kind == "receiving_departure":
            a, b = max(e["start"], h0), min(e["end"], h1)
            if b > a:
                busy[tr] = busy.get(tr, 0) + (b - a).total_seconds() / 60
    rd = [t for t, r in model.track_rows.items() if r.kind == "receiving_departure"]
    util = sum(busy.values()) / (len(rd) * 240) if rd else 0
    return {"delayed_trains": sum(1 for d in delays if d >= 5), "total_delay_min": round(sum(delays), 1),
            "max_delay_min": round(max(delays), 1) if delays else 0, "weighted_delay": round(weighted, 1),
            "conflicts": len(conflicts), "rd_utilization_4h": round(min(util, 1.5), 3)}
