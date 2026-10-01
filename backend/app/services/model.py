"""Снимок состояния станции для расчётов (проверка, конфликты, планирование).

IntervalBook — «книга занятости»: по каждому ключу ресурса хранит интервалы резервов
и блокировок (закрытия путей, окна обслуживания, вне смены, недостоверные данные).
Все проверки пересечений идут через неё, поэтому правило одно для всех модулей.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import aware, local_hm, utcnow
from app.domain.topology import Topology
from app.models import (
    CapacityRule, Incident, MaintenanceWindow, Operation, Plan, Reservation, Resource,
    ResourceShift, SimState, Station, TopologyNode, Track, TrackConnection, Train, TransferRequest,
    Wagon,
)

FAR = datetime(2100, 1, 1, tzinfo=timezone.utc)

KIND_LABEL = {
    "arrival": "Прибытие", "inspection": "Техосмотр", "shunting": "Манёвры", "loading": "Погрузка",
    "unloading": "Выгрузка", "repair": "Ремонт", "departure": "Отправление", "dwell": "Стоянка",
    "sorting": "Расформирование", "uncoupling": "Отцепка вагона",
}
RES_KIND_LABEL = {
    "shunting_loco": "маневровый локомотив", "loco_crew": "локомотивная бригада",
    "shunting_crew": "составительская бригада", "inspection_team": "бригада осмотрщиков",
    "cargo_equipment": "погрузочно-разгрузочный механизм", "repair_team": "ремонтная бригада",
}
MOVEMENT_KINDS = {"arrival", "departure", "shunting", "uncoupling"}


@dataclass(order=True)
class Entry:
    start: datetime
    end: datetime
    meta: dict = field(compare=False, default_factory=dict)


class IntervalBook:
    def __init__(self):
        self.data: dict[str, list[Entry]] = {}

    def add(self, key: str, start: datetime, end: datetime, **meta):
        if end <= start:
            return
        lst = self.data.setdefault(key, [])
        bisect.insort(lst, Entry(start, end, meta))

    def conflicts(self, key: str, start: datetime, end: datetime, ignore_train: str | None = None,
                  ignore_ops: set | None = None, kinds: set | None = None) -> list[Entry]:
        out = []
        for e in self.data.get(key, []):
            if e.start >= end:
                break
            if e.end <= start:
                continue
            if ignore_train and e.meta.get("train_id") == ignore_train:
                continue
            if ignore_ops and e.meta.get("op_id") in ignore_ops:
                continue
            if kinds and e.meta.get("type") not in kinds:
                continue
            out.append(e)
        return out

    def first_conflict(self, key, start, end, **kw) -> Entry | None:
        c = self.conflicts(key, start, end, **kw)
        return c[0] if c else None

    def copy(self) -> "IntervalBook":
        b = IntervalBook()
        b.data = {k: list(v) for k, v in self.data.items()}
        return b


def describe_block(e: Entry) -> str:
    m = e.meta
    t = m.get("type")
    span = f"{local_hm(e.start)}–{local_hm(e.end) if e.end < FAR else '…'}"
    if t == "reservation":
        return f"резерв «{m.get('label', 'операция')}» {span}"
    if t == "closure":
        return f"закрытие: {m.get('label', 'инцидент')} ({span})"
    if t == "maintenance":
        return f"окно обслуживания: {m.get('label', '')} ({span})"
    if t == "shift":
        return f"вне смены ({span})"
    if t == "faulty":
        return f"неисправность ресурса ({span})"
    if t == "data":
        return m.get("label", "нет достоверных данных о состоянии")
    if t == "restriction":
        return f"ограничение: {m.get('label', '')} ({span})"
    return span


class StationModel:
    """Загруженное из БД состояние основной станции на модельный момент now."""

    def __init__(self, db: Session, *, with_reservations: bool = True, data_states: dict | None = None):
        self.db = db
        sim = db.get(SimState, 1)
        self.sim = sim
        self.now: datetime = aware(sim.model_time)
        self.version: int = sim.plan_state_version
        self.station: Station = db.execute(select(Station).where(Station.kind == "main")).scalar_one()
        self.cfg: dict = self.station.config
        self.sid = self.station.id
        self.neighbors = {s.id: s for s in db.execute(select(Station).where(Station.kind == "neighbor")).scalars()}
        nodes = [dict(id=n.id, kind=n.kind, name=n.name, x=n.x, y=n.y, side=n.side)
                 for n in db.execute(select(TopologyNode)).scalars()]
        self.track_rows = {t.id: t for t in db.execute(select(Track)).scalars()}
        tracks = [dict(id=t.id, kind=t.kind, from_node=t.from_node, to_node=t.to_node, points=t.points,
                       useful_length_m=t.useful_length_m, number=t.number) for t in self.track_rows.values()]
        conns = [dict(id=c.id, from_node=c.from_node, to_node=c.to_node, kind=c.kind, track_id=c.track_id,
                      length_m=c.length_m, points=c.points) for c in db.execute(select(TrackConnection)).scalars()]
        self.topo = get_topology(self.sid, nodes, tracks, conns,
                                 (self.cfg.get("derived") or {}).get("schema_scale_u_per_m", 0.8))
        self.resources = {r.id: r for r in db.execute(select(Resource)).scalars()}
        self.shifts: dict[str, list[tuple[datetime, datetime]]] = {}
        for sh in db.execute(select(ResourceShift)).scalars():
            self.shifts.setdefault(sh.resource_id, []).append((aware(sh.start_at), aware(sh.end_at)))
        self.maintenance = list(db.execute(select(MaintenanceWindow)).scalars())
        self.incidents = list(db.execute(select(Incident).where(Incident.status == "active")).scalars())
        self.trains = {t.id: t for t in db.execute(select(Train)).scalars()}
        self.ops_by_train: dict[str, list[Operation]] = {}
        self.ops: dict[str, Operation] = {}
        for o in db.execute(select(Operation).order_by(Operation.train_id, Operation.seq)).scalars():
            self.ops[o.id] = o
            if o.train_id:
                self.ops_by_train.setdefault(o.train_id, []).append(o)
        self.wagons_by_train: dict[str, list[Wagon]] = {}
        for w in db.execute(select(Wagon)).scalars():
            if w.train_id:
                self.wagons_by_train.setdefault(w.train_id, []).append(w)
        self.rules = {r.code: r for r in db.execute(select(CapacityRule)).scalars()}
        self.plan: Plan | None = db.execute(select(Plan)).scalars().first()
        self.reservations: list[Reservation] = []
        if with_reservations:
            self.reservations = list(db.execute(select(Reservation).where(Reservation.status == "confirmed")).scalars())
        if data_states is None:
            from app.iot.quality import track_data_states
            data_states = track_data_states(db, utcnow(), self)
        self.data_states = data_states  # track_id -> {state, label, age_s, ...}

    # ----------------------------------------------------------- справочные
    def track_label(self, tid: str | None) -> str:
        if not tid or tid not in self.track_rows:
            return "—"
        t = self.track_rows[tid]
        return f"Главный путь {t.number}" if t.kind == "main" else f"Путь {t.number}"

    def track_label_lc(self, tid: str | None) -> str:
        """Подпись пути внутри предложения: «путь 3», «главный путь I»."""
        lab = self.track_label(tid)
        return lab[:1].lower() + lab[1:]

    def train_label(self, train_id: str | None) -> str:
        t = self.trains.get(train_id) if train_id else None
        return f"поезд № {t.number}" if t else "—"

    def zone_travel(self, a: str | None, b: str | None) -> int:
        tm = self.cfg.get("zone_travel_min", {})
        if not a or not b or a == b:
            return 0
        return int(tm.get(f"{a}-{b}", tm.get(f"{b}-{a}", tm.get("default", 5))))

    def op_zone(self, kind: str, track_id: str | None) -> str | None:
        if kind == "inspection":
            return "PTO"
        if track_id and self.track_rows.get(track_id) and self.track_rows[track_id].zone_id:
            return self.track_rows[track_id].zone_id
        return None

    def in_shift(self, rid: str, s: datetime, e: datetime) -> bool:
        sh = self.shifts.get(rid)
        if sh is None:
            return True
        return any(a <= s and e <= b for a, b in sh)

    def train_length(self, train: Train) -> float | None:
        if train.length_m:
            return train.length_m
        wagons = self.wagons_by_train.get(train.id, [])
        if not wagons or any(w.length_m is None for w in wagons):
            return None
        return sum(w.length_m for w in wagons) + (train.loco_length_m or 0)

    # ----------------------------------------------------------- книга интервалов
    def build_book(self, *, include_reservations: bool = True, include_data_blocks: bool = True) -> IntervalBook:
        b = IntervalBook()
        if include_reservations:
            for r in self.reservations:
                op = self.ops.get(r.operation_id) if r.operation_id else None
                label = r.purpose
                b.add(r.resource_key, aware(r.start_at), aware(r.end_at), type="reservation",
                      train_id=r.train_id, op_id=r.operation_id, label=label, reservation_id=r.id,
                      request_id=r.request_id, op_status=op.status if op else None)
        self.add_blocks(b, include_data_blocks=include_data_blocks)
        return b

    def add_blocks(self, b: IntervalBook, include_data_blocks: bool = True):
        for inc in self.incidents:
            s, e = aware(inc.start_at), aware(inc.end_at) or FAR
            if inc.kind == "track_closure" and inc.object_id:
                b.add(f"track:{inc.object_id}", s, e, type="closure", label=inc.title, incident_id=inc.id)
                # закрытый путь нельзя использовать и транзитом в маршрутах
            elif inc.kind == "switch_failure" and inc.object_id:
                b.add(f"switch:{inc.object_id}", s, e, type="closure", label=inc.title, incident_id=inc.id)
            elif inc.kind == "resource_failure" and inc.object_id:
                b.add(f"res:{inc.object_id}", s, e, type="faulty", label=inc.title, incident_id=inc.id)
            elif inc.kind == "neighbor_restriction" and inc.object_id:
                b.add(f"neighbor:{inc.object_id}", s, e, type="restriction", label=inc.title, incident_id=inc.id)
        for mw in self.maintenance:
            key = f"track:{mw.object_id}" if mw.object_type == "track" else (
                f"res:{mw.object_id}" if mw.object_type == "resource" else f"switch:{mw.object_id}")
            b.add(key, aware(mw.start_at), aware(mw.end_at), type="maintenance", label=mw.reason)
        for rid, r in self.resources.items():
            if r.status == "faulty":
                b.add(f"res:{rid}", self.now - timedelta(days=1), FAR, type="faulty", label=f"{r.name} неисправен")
            sh = sorted(self.shifts.get(rid, []))
            if sh:
                cur = self.now - timedelta(days=2)
                for a, z in sh:
                    if a > cur:
                        b.add(f"res:{rid}", cur, a, type="shift", label=f"{r.name}: вне смены")
                    cur = max(cur, z)
                b.add(f"res:{rid}", cur, FAR, type="shift", label=f"{r.name}: вне смены")
        if include_data_blocks:
            for tid, ds in self.data_states.items():
                if ds["state"] in ("stale", "missing", "contradictory", "invalid"):
                    b.add(f"track:{tid}", self.now - timedelta(hours=1), FAR, type="data",
                          label=ds["message"], data_state=ds["state"])

    # ----------------------------------------------------------- ресурсы по операциям
    def reservation_specs(self, train: Train, ops: list[Operation]) -> list[dict]:
        """Резервы для цепочки операций поезда. Используются при подтверждении, применении
        плана и в начальном заполнении — единое правило формирования броней."""
        return reservation_specs(self, train.id, train.number, ops)


def stays_of(ops: list) -> list[dict]:
    """Стоянки поезда: непрерывные интервалы занятия одного пути."""
    stays = []
    cur = None
    for o in ops:
        kind = o["kind"] if isinstance(o, dict) else o.kind
        track = o["track_id"] if isinstance(o, dict) else o.track_id
        start = o["start"] if isinstance(o, dict) else aware(o.planned_start)
        end = o["end"] if isinstance(o, dict) else aware(o.planned_end)
        oid = o.get("id") if isinstance(o, dict) else o.id
        from_track = o.get("from_track_id") if isinstance(o, dict) else o.from_track_id
        if kind == "uncoupling":  # отцепка: состав остаётся на своём пути
            track = from_track
        if kind in ("arrival",) or (kind == "shunting"):
            if cur and kind == "shunting":
                cur["end"] = end  # путь отправления занят до окончания вытягивания состава
                cur["ops"].append(oid)
                stays.append(cur)
            elif kind == "shunting" and from_track:
                stays.append({"track_id": from_track, "start": start, "end": end, "ops": [oid]})
            cur = {"track_id": track, "start": start, "end": end, "ops": [oid]}
        else:
            if cur is None:  # предыдущие операции уже выполнены — стоянка продолжается
                cur = {"track_id": track, "start": start, "end": end, "ops": []}
            cur["end"] = end
            cur["ops"].append(oid)
            if kind == "departure":
                stays.append(cur)
                cur = None
    if cur:
        stays.append(cur)
    return stays


def reservation_specs(model: "StationModel", train_id: str, train_number: str, ops: list) -> list[dict]:
    """Стоянки считаются по всей цепочке (включая выполненные операции), чтобы путь под
    стоящим поездом оставался зарезервированным с момента фактического прибытия, даже если
    следующая операция начнётся позже. Полностью завершённые стоянки не резервируются."""
    buf = timedelta(minutes=model.cfg["processing"].get("track_buffer_min", 5))
    specs = []
    norm = []
    for o in ops:
        if isinstance(o, dict):
            norm.append(o)
        else:
            done = o.status == "done"
            norm.append({"id": o.id, "kind": o.kind, "track_id": o.track_id, "from_track_id": o.from_track_id,
                         "side": o.side,
                         "start": aware(o.actual_start if (done or o.status == "in_progress") and o.actual_start else o.planned_start),
                         "end": aware(o.actual_end if done and o.actual_end else o.planned_end),
                         "resource_ids": o.resource_ids or [], "route_nodes": o.route_nodes or [],
                         "status": o.status})
    norm = [o for o in norm if o.get("status") != "cancelled"]
    status = {o["id"]: o.get("status") for o in norm}
    for st in stays_of(norm):
        if all(status.get(i) == "done" for i in st["ops"]):
            continue
        specs.append({"key": f"track:{st['track_id']}", "start": st["start"], "end": st["end"] + buf,
                      "purpose": f"Занятие {model.track_label_lc(st['track_id'])} поездом № {train_number}",
                      "operation_id": next((i for i in st["ops"] if status.get(i) != "done"), st["ops"][0]),
                      "train_id": train_id})
    for o in norm:
        if o.get("status") == "done":
            continue
        label = f"{KIND_LABEL.get(o['kind'], o['kind'])} п. № {train_number}"
        if o["kind"] == "uncoupling" and o.get("track_id"):
            specs.append({"key": f"track:{o['track_id']}", "start": o["start"], "end": o["end"] + buf,
                          "purpose": f"Подача неисправного вагона п. № {train_number} в депо",
                          "operation_id": o["id"], "train_id": train_id})
        if o["kind"] in MOVEMENT_KINDS:
            for tt in through_tracks(model, o):
                specs.append({"key": f"track:{tt}", "start": o["start"], "end": o["end"],
                              "purpose": f"Проследование: {label}", "operation_id": o["id"], "train_id": train_id})
            for sw in o.get("route_nodes") or []:
                specs.append({"key": f"switch:{sw}", "start": o["start"], "end": o["end"], "purpose": label,
                              "operation_id": o["id"], "train_id": train_id})
        for rid in o.get("resource_ids") or []:
            r = model.resources.get(rid)
            pad = timedelta(minutes=model.zone_travel(r.home_zone_id if r else None,
                                                      model.op_zone(o["kind"], o["track_id"])))
            specs.append({"key": f"res:{rid}", "start": o["start"] - pad, "end": o["end"], "purpose": label,
                          "operation_id": o["id"], "train_id": train_id})
    return specs


def with_presence(model: "StationModel", train, live: list[dict]) -> list[dict]:
    """Добавляет псевдооперацию «стоянка с текущего момента» для поезда, уже стоящего на пути:
    занятость пути начинается сейчас, а не с его следующей операции."""
    if train is None or train.status != "on_station" or not train.current_track_id or not live:
        return live
    first = live[0]
    if first["kind"] == "arrival":
        return live
    pseudo = {"id": f"presence-{train.id}", "kind": "dwell", "track_id": train.current_track_id,
              "from_track_id": None, "start": min(model.now, first["start"]), "end": min(model.now, first["start"]),
              "status": "in_progress", "resource_ids": [], "route_nodes": [], "train_id": train.id, "side": None}
    return [pseudo] + live


def through_tracks(model, o: dict) -> list[str]:
    """Пути, по которым маршрут движения проходит транзитом (кроме исходного и целевого)."""
    topo = model.topo
    k = o["kind"]
    if k == "arrival":
        r = topo.arrival_route(o.get("side") or "west", o["track_id"])
    elif k == "departure":
        r = topo.departure_route(o.get("side") or "east", o["track_id"])
    else:
        r = topo.shunting_route(o.get("from_track_id"), o["track_id"]) if o.get("from_track_id") else None
    if not r:
        return []
    return [t for t in r.through_track_ids if t not in (o["track_id"], o.get("from_track_id"))]


_TOPO_CACHE: dict = {}


def get_topology(sid, nodes, tracks, conns, scale: float = 0.8) -> Topology:
    key = (sid, len(nodes), len(tracks), len(conns), scale)
    if key not in _TOPO_CACHE:
        _TOPO_CACHE.clear()
        _TOPO_CACHE[key] = Topology(sid, nodes, tracks, conns, scale)
    return _TOPO_CACHE[key]


def requests_in_month(db: Session, month: str) -> list[TransferRequest]:
    return list(db.execute(select(TransferRequest)).scalars())
