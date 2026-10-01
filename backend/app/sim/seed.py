"""Воспроизводимое заполнение демо-данными.

Начальное состояние полностью определяется (конфигурация станции, сценарий, seed).
Расписание размещается тем же алгоритмом, что и проверка заявок, поэтому начальный план
не содержит пересечений, а резервы проходят ограничение-исключение БД.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.core.timeutil import UTC, utcnow
from app.domain.topology import build_topology, load_config, point_at, poly_len
from app.models import (
    CapacityRule, Device, IndexConfig, MaintenanceWindow, Operation, Park, Plan, Resource, ResourceShift, SimState,
    Station, TopologyNode, Track, TrackConnection, Train, TransferRequest, User, Wagon, Zone,
)
from app.services.model import StationModel
from app.services.placement import Placer, TrainSpec, commit_to_book

log = logging.getLogger("seed")

SIM_DATE = "2026-10-01"
SIM_START_LOCAL_H = 12.0

OPERATIONAL_TABLES = [
    "reservations", "operations", "wagons", "trains", "transfer_requests", "incidents", "recommendations",
    "plan_versions", "maintenance_windows", "resource_shifts", "resources", "capacity_rules", "plans",
    "devices", "observations", "telemetry_events", "telemetry_rejects", "manual_overrides",
    "track_connections", "tracks", "topology_nodes", "zones", "parks", "stations", "state_snapshots",
    "state_deltas", "index_snapshots", "domain_events", "idempotency_keys",
    # процесс работников (сброс демо-мира); привязки пользователей user_scopes сохраняются
    "defect_reports", "work_orders", "work_inspections", "work_events", "attachments", "notifications",
]

DEMO_USERS = [
    ("u-train", "train", "Ахметов А. (поездной диспетчер)", "train_dispatcher"),
    ("u-station", "station", "Сейтова Г. (станционный диспетчер)", "station_dispatcher"),
    ("u-duty", "duty", "Ким В. (дежурный по станции)", "duty_officer"),
    ("u-admin", "admin", "Администратор стенда", "admin"),
    ("u-viewer", "viewer", "Наблюдатель (жюри)", "observer"),
    # мобильный раздел работников (/mobile)
    ("u-insp", "inspector", "Бекова А. (осмотрщик вагонов)", "wagon_inspector"),
    ("u-repairer", "repairer", "Нурланов Е. (осмотрщик-ремонтник)", "wagon_inspector_repairer"),
    ("u-pto", "pto", "Жумабаева С. (оператор ПТО)", "pto_operator"),
    ("u-fitter", "fitter", "Ли Д. (слесарь по ремонту подвижного состава)", "rolling_stock_fitter"),
    ("u-fitter2", "fitter2", "Сапаров М. (слесарь, бригада 2)", "rolling_stock_fitter"),
    ("u-senior", "senior", "Омаров Т. (старший осмотрщик-ремонтник)", "senior_wagon_inspector"),
    ("u-senior2", "senior2", "Ким Р. (старший осмотрщик-ремонтник)", "senior_wagon_inspector"),
    ("u-otr", "otr_inspector", "Ахмедов Б. (осмотрщик, станция Отар)", "wagon_inspector"),
]
# привязка демо-работников: (ПТО, бригада); u-otr — другая станция (проверка изоляции данных)
DEMO_SCOPES = {"u-insp": ("PTO", None), "u-repairer": ("PTO", "BR-1"), "u-pto": ("PTO", None),
               "u-fitter": ("PTO", "BR-1"), "u-fitter2": ("PTO", "BR-2"), "u-senior": ("PTO", "BR-1"),
               "u-senior2": ("PTO", None), "u-otr": ("PTO-OTR", None)}
SPARE_WAGONS = [("59900011", "gondola"), ("59900029", "gondola"), ("59900037", "covered"), ("59900045", "covered"),
                ("59900052", "tank"), ("59900060", "hopper")]
DEMO_PASSWORD = "demo123"

DEFAULT_INDEX_CONFIG = {
    "weights": {"throughput": 0.25, "schedule_deviation": 0.25, "track_utilization": 0.20,
                "route_conflicts": 0.15, "idle": 0.15},
    "params": {
        "throughput": {"window_min": 240, "target_wagons_per_hour": 90},
        "schedule_deviation": {"window_min": 240, "max_avg_delay_min": 45},
        "track_utilization": {"target_low": 0.45, "target_high": 0.80, "zero_at_low": 0.0, "zero_at_high": 1.0},
        "route_conflicts": {"max_conflicts": 8},
        "idle": {"window_min": 120, "reserve_share": 0.25},
    },
    "thresholds": {"normal": 75, "attention": 50},
    "max_data_age_s": 30,
}


REALTIME_WINDOW_H = 4.0  # реальное время: расписание пополняется окнами по 4 ч
_CURRENT_DAY: datetime | None = None  # «день сценария» текущего сброса (реальное время)


def start_time(cfg: dict, real_time: bool = False) -> datetime:
    """Начало модели. Демо — фиксированные сутки (воспроизводимо); реальное время — текущий момент."""
    if real_time:
        return utcnow().replace(second=0, microsecond=0)
    tzi = ZoneInfo(cfg["station"].get("timezone", "Asia/Almaty"))
    base = datetime.fromisoformat(SIM_DATE).replace(tzinfo=tzi)
    return (base + timedelta(hours=SIM_START_LOCAL_H)).astimezone(UTC)


def local_day(cfg, sim=None) -> datetime:
    """Опорные «сутки» сценария: события задаются часами от их начала.
    В реальном времени сутки сдвинуты так, что 12:00 сценария = момент запуска: сценарии
    сохраняют свои интервалы относительно старта."""
    iso = ((sim.world or {}).get("sim_day") if sim is not None else None)
    if iso:
        return datetime.fromisoformat(iso)
    if _CURRENT_DAY is not None:
        return _CURRENT_DAY
    tzi = ZoneInfo(cfg["station"].get("timezone", "Asia/Almaty"))
    return datetime.fromisoformat(SIM_DATE).replace(tzinfo=tzi)


def ensure_users(db: Session):
    from app.services.admin import ensure_system_roles
    ensure_system_roles(db)
    existing = {u for (u,) in db.execute(select(User.id))}
    names = {u for (u,) in db.execute(select(User.username))}
    for uid, un, fn, role in DEMO_USERS:  # недостающие демо-учётки добавляются; изменённые администратором не трогаются
        if uid not in existing and un not in names:
            db.add(User(id=uid, username=un, full_name=fn, role=role, password_hash=hash_password(DEMO_PASSWORD)))
    db.flush()


def ensure_demo_scopes(db: Session, station_id: str):
    """Привязка демо-работников к станции, ПТО и бригаде (у созданных администратором — своя)."""
    from app.models import UserScope
    for uid, (pto, brigade) in DEMO_SCOPES.items():
        if not db.get(User, uid):
            continue
        st = "OTR" if uid == "u-otr" else station_id
        sc = db.get(UserScope, uid)
        if sc is None:
            db.add(UserScope(user_id=uid, station_id=st, pto_id=pto, brigade_id=brigade))
        elif uid != "u-otr":
            sc.station_id = station_id  # смена конфигурации станции — демо-работники переходят вместе с ней
    db.flush()


def seed_spare_wagons(db: Session, cfg: dict):
    """Резерв исправных порожних вагонов на станции — кандидаты для замены неисправного вагона."""
    sid = cfg["station"]["id"]
    tracks = list(db.execute(select(Track).where(Track.station_id == sid)).scalars())
    place = next((t for t in tracks if t.kind == "repair"), None) or next((t for t in tracks if t.kind in ("sorting", "cargo")), None)
    for num, kind in SPARE_WAGONS:
        db.add(Wagon(id=f"W{num}", train_id=None, number=num, kind=kind, length_m=WAGON_LEN[kind], loaded=False,
                     condition="ok", position=0, track_id=place.id if place else None))
    db.flush()


def seed_demo_reports(db: Session, cfg: dict):
    """Два воспроизводимых сообщения осмотрщика в очереди (обычное и срочное)."""
    from app.services import workflow as wf
    insp = db.get(User, "u-insp")
    if not insp:
        return
    trains = sorted(db.execute(select(Train).where(Train.status == "on_station")).scalars(), key=lambda t: t.number)
    if not trains:
        return
    t = trains[0]
    wl = sorted(db.execute(select(Wagon).where(Wagon.train_id == t.id)).scalars(), key=lambda w: w.position)
    samples = [(2, "brake", "Тормозная колодка", "Тормозная колодка изношена до предельной толщины.", "normal"),
               (5, "axlebox", "Букса правая, 2-я колёсная пара", "Повышенный нагрев буксы, следы смазки на корпусе.", "urgent")]
    for i, (pos, cat, comp, text, urg) in enumerate(samples):
        if pos > len(wl):
            continue
        w = wl[pos - 1]
        wf.submit_defect(db, insp, {"client_uuid": f"seed-{cfg['station']['id']}-{i}", "wagon_id": w.id, "wagon_number": w.number,
                                    "train_id": t.id, "track_id": t.current_track_id, "position": w.position,
                                    "category": cat, "component": comp, "description": text, "urgency": urg,
                                    "attachment_ids": [], "location": None})
    db.flush()


def _clear_attachment_files():
    """Сброс демо-мира удаляет и файлы вложений (метаданные очищены TRUNCATE)."""
    import os
    from app.config import get_settings
    d = get_settings().attachments_dir
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                os.remove(p)
    except FileNotFoundError:
        pass


def ensure_index_config(db: Session):
    if not db.execute(select(IndexConfig)).first():
        db.add(IndexConfig(version=1, config=DEFAULT_INDEX_CONFIG, created_at=utcnow(), created_by="seed", active=True))


def wipe(db: Session):
    db.execute(text("TRUNCATE " + ", ".join(OPERATIONAL_TABLES) + " RESTART IDENTITY CASCADE"))


def seed_static(db: Session, cfg: dict, rnd: random.Random, t0: datetime):
    topo = build_topology(cfg)
    sid = cfg["station"]["id"]
    cfg = {**cfg, "derived": topo["geometry"]}  # масштаб схемы — общий для путей и составов
    db.add(Station(id=sid, name=cfg["station"]["name"], kind="main", is_demo=True,
                   timezone=cfg["station"].get("timezone", "Asia/Almaty"), config=cfg))
    day = local_day(cfg)
    for n in cfg["neighbors"]:
        occ = []
        for _ in range(n.get("receiving_tracks", 2)):
            ints = []
            h = rnd.uniform(9, 11)
            while h < 22:
                d = rnd.uniform(1.0, 2.5)
                ints.append([(day + timedelta(hours=h)).isoformat(), (day + timedelta(hours=h + d)).isoformat()])
                h += d + rnd.uniform(0.7, 2.5)
            occ.append(ints)
        db.add(Station(id=n["id"], name=n["name"], kind="neighbor", is_demo=True, timezone="Asia/Almaty",
                       config={**n, "occupancy": occ}))
    db.flush()
    for p in topo["parks"]:
        db.add(Park(id=p["id"], station_id=sid, name=p["name"], kind=p["kind"]))
    for n in topo["nodes"]:
        db.add(TopologyNode(station_id=sid, **n))
    db.flush()
    for t in topo["tracks"]:
        db.add(Track(station_id=sid, name=t["name"], **{k: v for k, v in t.items() if k != "name"}))
    for c in topo["connections"]:
        db.add(TrackConnection(station_id=sid, **c))
    for z in topo["zones"]:
        db.add(Zone(id=z["code"], station_id=sid, name=z["name"], kind=z["kind"], track_ids=z["track_ids"],
                    x=z["x"], y=z["y"], params={**z["params"], "display_id": z["id"]}))
    shifts = cfg.get("shifts", {})
    for r in cfg["resources"]:
        db.add(Resource(id=r["id"], station_id=sid, kind=r["kind"], name=r["name"], home_zone_id=r.get("zone"),
                        status="available", params={"shift": r.get("shift")}))
    db.flush()
    for r in cfg["resources"]:
        if r.get("shift") and r["shift"] in shifts:
            a, b = shifts[r["shift"]]
            for dd in (-1, 0, 1):
                s = day + timedelta(days=dd, hours=a)
                e = day + timedelta(days=dd, hours=b)
                db.add(ResourceShift(resource_id=r["id"], start_at=s.astimezone(UTC), end_at=e.astimezone(UTC)))
    for i, mw in enumerate(cfg.get("maintenance", [])):
        if mw["object_type"] == "track":
            oid = f"{sid}-T{mw['track']}"
        else:
            oid = mw["resource"]
        db.add(MaintenanceWindow(id=f"MW-{i + 1}", station_id=sid, object_type=mw["object_type"], object_id=oid,
                                 start_at=(day + timedelta(hours=mw["start_h"])).astimezone(UTC),
                                 end_at=(day + timedelta(hours=mw["end_h"])).astimezone(UTC), reason=mw["reason"]))
    for r in cfg["capacity_rules"]:
        db.add(CapacityRule(id=f"{sid}-{r['code']}", station_id=sid, code=r["code"], name=r["name"],
                            category=r["category"], unit=r["unit"], period=r["period"], source=r["source"],
                            rule_text=r["rule"], value=r.get("value"), policy=r.get("policy", "hard"), active=True))
    pl = cfg["plan"]
    month = t0.astimezone(ZoneInfo(cfg["station"].get("timezone", "Asia/Almaty"))).strftime("%Y-%m")
    db.add(Plan(id=f"{sid}-PLAN-{month}", station_id=sid, month=month,
                target_wagons=pl["month_target_wagons"], actual_base_wagons=pl["actual_base_wagons"],
                policy=pl.get("policy", "soft")))
    seed_devices(db, cfg, topo)
    db.flush()
    return topo


def seed_devices(db: Session, cfg: dict, topo: dict):
    sid = cfg["station"]["id"]
    for t in topo["tracks"]:
        x, y, _ = point_at(t["points"], poly_len(t["points"]) / 2)  # середина пути по геометрии
        db.add(Device(id=f"{sid}-TC-{t['number']}", name=f"Рельсовая цепь, {t['name'].lower()}", kind="track_circuit",
                      station_id=sid, object_id=t["id"], allowed_event_types=["occupancy", "heartbeat"],
                      period_s=2, stale_after_s=8, x=x, y=y))
    for n in topo["nodes"]:
        if n["kind"] == "switch":
            db.add(Device(id=f"{sid}-SW-{n['id'].split('-')[-1]}", name=f"Контроль положения, {n['name']}",
                          kind="switch_sensor", station_id=sid, object_id=n["id"],
                          allowed_event_types=["switch_position", "heartbeat"], period_s=5, stale_after_s=20,
                          x=n["x"], y=n["y"]))
        if n["kind"] == "entry":
            side = "W" if n["side"] == "west" else "E"
            db.add(Device(id=f"{sid}-RFID-{side}", name=f"RFID-считыватель, {n['name'].lower()}", kind="rfid_reader",
                          station_id=sid, object_id=n["id"], allowed_event_types=["rfid_read", "heartbeat"],
                          period_s=5, stale_after_s=30, x=n["x"], y=n["y"]))
    for r in cfg["resources"]:
        if r["kind"] == "shunting_loco":
            db.add(Device(id=f"{sid}-GPS-{r['id']}", name=f"ГНСС-трекер, {r['name']}", kind="loco_gps", station_id=sid,
                          object_id=r["id"], allowed_event_types=["position", "heartbeat"], period_s=1,
                          stale_after_s=6, x=0, y=0))
        if r["kind"] == "cargo_equipment":
            db.add(Device(id=f"{sid}-EQ-{r['id']}", name=f"Контроллер, {r['name']}", kind="cargo_equipment",
                          station_id=sid, object_id=r["id"], allowed_event_types=["equipment_state", "heartbeat"],
                          period_s=5, stale_after_s=20, x=0, y=0))
    zone_ids = [z["code"] for z in topo["zones"]]
    if "PTO" in zone_ids:
        db.add(Device(id=f"{sid}-DIAG-PTO", name="Диагностический комплекс ПТО (буксы, гребни)", kind="repair_diag",
                      station_id=sid, object_id="PTO", allowed_event_types=["diagnostics", "heartbeat"], period_s=5,
                      stale_after_s=30, x=0, y=0))


# ----------------------------------------------------------------------------- расписание
WAGON_LEN = {"gondola": 13.92, "covered": 17.64, "tank": 12.02, "flat": 14.62, "hopper": 14.72, "passenger": 24.5}


class TimetableBuilder:
    def __init__(self, db: Session, cfg: dict, rnd: random.Random, t0: datetime, include_existing: bool = False):
        self.db, self.cfg, self.rnd, self.t0 = db, cfg, rnd, t0
        self.model = StationModel(db, with_reservations=include_existing, data_states={})
        self.book = self.model.build_book(include_reservations=include_existing, include_data_blocks=False)
        self.trains: list[tuple[Train, list[Operation]]] = []
        self.counter = {"transit": 2001, "transfer_in": 3001, "cargo": 2501, "passenger": 101}
        self.wagon_no = 50000000 + rnd.randint(0, 999) * 1000
        if include_existing:
            # продолжение расписания: номера поездов и вагонов не повторяются
            from sqlalchemy import func
            nums = [int(n) for (n,) in db.execute(select(Train.number)) if str(n).isdigit()]
            for tpl, (lo, hi) in {"transit": (2001, 2499), "cargo": (2501, 2999), "transfer_in": (3001, 3999),
                                  "passenger": (101, 199)}.items():
                used = [n for n in nums if lo <= n <= hi]
                self.counter[tpl] = (max(used) + (1 if tpl == "passenger" else 2)) if used else lo
            wmax = db.execute(select(func.max(Wagon.number))).scalar()
            if wmax and str(wmax).isdigit():
                self.wagon_no = max(self.wagon_no, int(wmax) + 1)

    def _wagons(self, n: int, kinds: list[str]) -> list[Wagon]:
        out = []
        for i in range(n):
            self.wagon_no += self.rnd.randint(1, 37)
            k = self.rnd.choice(kinds)
            out.append(Wagon(id=f"W{self.wagon_no}", number=str(self.wagon_no), kind=k, length_m=WAGON_LEN[k],
                             loaded=self.rnd.random() < 0.7, condition="ok", position=i + 1))
        return out

    def add(self, tpl: str, arrival: datetime, *, wagons: int | None = None, side_in: str | None = None,
            dwell_min: int | None = None, priority: int | None = None, number: str | None = None,
            wagon_kinds: list[str] | None = None, exact: bool = False, fixed_tracks: list[str] | None = None,
            origin: str | None = None) -> tuple[Train, list[Operation]] | None:
        cfg, rnd = self.cfg, self.rnd
        steps = [dict(s) for s in cfg["processing"]["templates"][tpl]]
        kind = {"transit": "freight", "transfer_in": "transfer", "cargo": "freight", "passenger": "passenger"}[tpl]
        side_in = side_in or rnd.choice(["west", "east"])
        side_out = "east" if side_in == "west" else "west"
        if wagons is None:
            wagons = {"transit": rnd.randint(42, 56), "transfer_in": rnd.randint(30, 44), "cargo": rnd.randint(16, 20),
                      "passenger": 12}[tpl]
        kinds = wagon_kinds or (["passenger"] if tpl == "passenger" else
                                (["gondola"] if tpl == "cargo" else ["gondola", "covered", "tank", "hopper"]))
        wl = self._wagons(wagons, kinds)
        loco = 20.0 if tpl == "passenger" else 34.0
        length = round(sum(w.length_m for w in wl) + loco, 1)
        num = number or str(self.counter[tpl])
        if not number:
            self.counter[tpl] += 2 if tpl != "passenger" else 1
            while self.db.get(Train, f"TR-{num}") is not None:  # номер занят (длительная работа)
                num = str(self.counter[tpl])
                self.counter[tpl] += 2 if tpl != "passenger" else 1
        dep_nb = None
        if tpl in ("transit", "cargo", "passenger"):
            proc = sum(s["duration"] for s in steps if s["kind"] != "departure")
            dwell = dwell_min if dwell_min is not None else (0 if tpl == "passenger" else rnd.randint(10, 40))
            dep_nb = arrival + timedelta(minutes=proc + dwell)
        tid = f"TR-{num}"
        pr = priority or {"passenger": 5, "transit": 3, "transfer_in": 2, "cargo": 2}[tpl]
        spec = TrainSpec(train_id=tid, number=num, kind=kind, priority=pr, length_m=length, side_in=side_in,
                         side_out=side_out, template=steps, arrival=arrival, departure_not_before=dep_nb, wagons=wagons)
        placer = Placer(self.model, self.book, wait_max_min=150 if not exact else 120)
        if fixed_tracks:
            orig = placer.candidates
            placer.candidates = lambda g, sp, st, res, _o=orig: [t for t in _o(g, sp, st, res) if t in fixed_tracks] \
                if g == "receiving_departure" else _o(g, sp, st, res)
        res = placer.place(spec, arrival_exact=exact)
        if not res.ok:
            log.warning("Поезд %s не размещён: %s", num, res.track_reasons)
            return None
        commit_to_book(self.model, self.book, tid, num, res.ops)
        neighbor_in = [n["id"] for n in cfg["neighbors"] if n["side"] == side_in]
        neighbor_out = [n["id"] for n in cfg["neighbors"] if n["side"] == side_out]
        dep_op = next((o for o in res.ops if o.kind == "departure"), None)
        train = Train(id=tid, number=num, kind=kind, priority=pr, origin_station_id=origin or (neighbor_in[0] if neighbor_in else None),
                      destination_station_id=(neighbor_out[0] if neighbor_out and tpl != "transfer_in" else cfg["station"]["id"]),
                      arrival_side=side_in, departure_side=side_out, wagons_count=wagons, length_m=None,
                      loco_length_m=loco, status="scheduled", scheduled_arrival=arrival, expected_arrival=arrival,
                      scheduled_departure=dep_nb if dep_op else None,
                      expected_departure=dep_op.start if dep_op else None, cargo=None)
        for w in wl:
            w.train_id = tid
        ops = []
        sid = cfg["station"]["id"]
        for o in res.ops:
            ops.append(Operation(id=f"OP-{num}-{o.seq}", station_id=sid, train_id=tid, kind=o.kind, seq=o.seq,
                                 track_id=o.track_id, from_track_id=o.from_track_id, side=o.side,
                                 duration_min=o.duration_min, requirements=o.requirements, resource_ids=o.resource_ids,
                                 route_nodes=o.route_nodes, planned_start=o.start, planned_end=o.end,
                                 not_before=(arrival if o.kind == "arrival" else (dep_nb if o.kind == "departure" else None)),
                                 status="planned", reserved=True))
        self.db.add(train)
        self.db.flush()
        self.db.add_all(wl)
        self.db.add_all(ops)
        self.trains.append((train, ops))
        return train, ops

    def random_timetable(self, counts: dict, span_h: float, *, start_offset_h: float = -2.5,
                         cluster: tuple | None = None):
        """counts: шаблон -> число поездов. cluster: (шаблон, n, от_ч, до_ч) — пик прибытий."""
        plan = []
        for tpl, n in counts.items():
            for _ in range(n):
                plan.append((tpl, self.t0 + timedelta(hours=self.rnd.uniform(start_offset_h, span_h + start_offset_h))))
        if cluster:
            tpl, n, a, b = cluster
            for _ in range(n):
                plan.append((tpl, self.t0 + timedelta(hours=self.rnd.uniform(a, b))))
        plan.sort(key=lambda p: (p[0] != "passenger", p[1]))
        for tpl, arr in plan:
            self.add(tpl, arr.replace(second=0, microsecond=0))

    def finalize(self):
        """Привести статусы к моменту начала: прошедшие операции — выполнены, текущие — выполняются."""
        t0 = self.t0
        for train, ops in self.trains:
            last_track = None
            for o in ops:
                if o.planned_end <= t0:
                    o.status, o.actual_start, o.actual_end = "done", o.planned_start, o.planned_end
                    last_track = o.track_id if o.kind != "departure" else None
                    if o.kind == "uncoupling":
                        last_track = o.from_track_id
                elif o.planned_start <= t0:
                    o.status, o.actual_start = "in_progress", o.planned_start
                    last_track = o.from_track_id if o.kind in ("shunting", "departure") else o.track_id
                o.forecast_start, o.forecast_end = o.planned_start, o.planned_end
            if all(o.status == "done" for o in ops):
                train.status = "departed" if any(o.kind == "departure" for o in ops) else "completed"
                train.current_track_id = None
            elif any(o.status != "planned" for o in ops):
                train.status = "on_station"
                train.current_track_id = last_track
            else:
                train.status = "approaching" if ops[0].planned_start - t0 < timedelta(minutes=40) else "scheduled"
        self.db.flush()
        from app.services.reservations import create_from_ops
        for train, ops in self.trains:
            create_from_ops(self.db, self.model, train.id, train.number, ops)


def reset_world(db: Session, config_name: str, scenario: str, seed: int, real_time: bool = False):
    from app.sim.scenarios import SCENARIOS, apply_scenario
    if scenario not in SCENARIOS:
        raise ValueError(f"Неизвестный сценарий {scenario}")
    cfg = load_config(config_name)
    rnd = random.Random(seed)
    wipe(db)
    ensure_users(db)
    ensure_index_config(db)
    global _CURRENT_DAY
    t0 = start_time(cfg, real_time)
    tzi = ZoneInfo(cfg["station"].get("timezone", "Asia/Almaty"))
    _CURRENT_DAY = (t0 - timedelta(hours=SIM_START_LOCAL_H)).astimezone(tzi) if real_time else None
    sim = db.get(SimState, 1)
    if sim is None:
        sim = SimState(id=1, model_time=t0)
        db.add(sim)
    sim.model_time, sim.speed, sim.running, sim.seed = t0, 10.0, False, seed
    sim.scenario, sim.station_config = scenario, config_name
    sim.plan_state_version = (sim.plan_state_version or 0) + 1
    sim.world, sim.scheduled_events = {"device_faults": {}}, []
    if real_time:
        tt = cfg.get("timetable", {})
        # реальное время: ×1 по часам сервера, расписание пополняется (extend_timetable)
        sim.world = {"device_faults": {}, "real_time": True, "sim_day": _CURRENT_DAY.isoformat(),
                     "tt_until": (t0 + timedelta(hours=tt.get("span_h", 10) - 2.5)).isoformat()}
        sim.speed, sim.running = 1.0, True
    db.flush()
    seed_static(db, cfg, rnd, t0)
    tb = TimetableBuilder(db, cfg, rnd, t0)
    apply_scenario(db, tb, cfg, scenario, rnd)
    db.flush()
    ensure_demo_scopes(db, cfg["station"]["id"])
    seed_spare_wagons(db, cfg)
    if not scenario.startswith("demo_"):
        seed_demo_reports(db, cfg)
    _clear_attachment_files()
    from app.config import get_settings
    if get_settings().seed_optimize and not scenario.startswith("demo_"):
        optimize_initial_plan(db)
    _CURRENT_DAY = None
    return sim


def _extend_shifts(db: Session, cfg: dict, need_until: datetime):
    """Смены бригад и ресурсов продлеваются по суткам вперёд (реальное время работает дольше суток)."""
    from sqlalchemy import func
    tzi = ZoneInfo(cfg["station"].get("timezone", "Asia/Almaty"))
    shifts = cfg.get("shifts", {})
    for r in cfg["resources"]:
        if not r.get("shift") or r["shift"] not in shifts:
            continue
        a, b = shifts[r["shift"]]
        row = db.execute(select(ResourceShift.start_at, ResourceShift.end_at).where(ResourceShift.resource_id == r["id"])
                         .order_by(ResourceShift.start_at.desc()).limit(1)).first()
        if row is None:
            continue
        last = row[1]
        # сутки начала последней смены (ночная смена заканчивается на следующие сутки)
        day = (row[0].astimezone(tzi) - timedelta(hours=a)).replace(hour=0, minute=0, second=0, microsecond=0)
        while last < need_until:
            day += timedelta(days=1)
            st_, en_ = day + timedelta(hours=a), day + timedelta(hours=b)
            db.add(ResourceShift(resource_id=r["id"], start_at=st_.astimezone(UTC), end_at=en_.astimezone(UTC)))
            last = en_.astimezone(UTC)


def extend_timetable(db: Session, sim: SimState, now: datetime) -> int:
    """Реальное время: когда до конца расписания меньше окна, добавляются поезда на следующее
    окно (REALTIME_WINDOW_H) с размещением по текущим резервам — новые поезда не создают
    двойных бронирований. Возвращает число добавленных поездов."""
    w = sim.world or {}
    if not w.get("real_time") or not w.get("tt_until"):
        return 0
    until = datetime.fromisoformat(w["tt_until"])
    if until - now > timedelta(hours=REALTIME_WINDOW_H):
        return 0
    cfg = load_config(sim.station_config)
    tt = cfg.get("timetable", {})
    span = tt.get("span_h", 10)
    rnd = random.Random(sim.seed * 100003 + int(until.timestamp()) // 60)
    tb = TimetableBuilder(db, cfg, rnd, until, include_existing=True)
    for tpl in ("passenger", "transit", "transfer_in", "cargo"):
        rate = tt.get(tpl, 0) * REALTIME_WINDOW_H / span
        n = int(rate) + (1 if rnd.random() < rate - int(rate) else 0)
        for _ in range(n):
            arr = until + timedelta(hours=rnd.uniform(0, REALTIME_WINDOW_H))
            tb.add(tpl, arr.replace(second=0, microsecond=0))
    tb.t0 = now
    tb.finalize()
    _extend_shifts(db, cfg, until + timedelta(hours=REALTIME_WINDOW_H + 24))
    sim.world = {**w, "tt_until": (until + timedelta(hours=REALTIME_WINDOW_H)).isoformat()}
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(sim, "world")
    return len(tb.trains)


def optimize_initial_plan(db: Session) -> int:
    """Начальное расписание строится жадно и содержит устранимые задержки. Для рабочих сценариев
    оно один раз оптимизируется CP-SAT в детерминированном режиме (воспроизводимо по seed), чтобы
    последующие пересчёты после инцидентов меняли только то, что действительно затронуто.
    Фикстуры демонстрации заявки (demo_*) не оптимизируются: их состояние задано намеренно."""
    from sqlalchemy import delete
    from app.models import Reservation
    from app.services.forecast import forecast
    from app.services.planner import PlanBuilder, verify
    from app.services.reservations import create_from_ops
    import hashlib
    from datetime import datetime as _dt
    from app.models import SeedPlanCache
    model = StationModel(db, data_states={})
    sig = "|".join(f"{o.id}:{o.planned_start.isoformat()}:{o.track_id}:{o.status}:{','.join(o.resource_ids or [])}"
                   for o in sorted(model.ops.values(), key=lambda x: x.id))
    key = hashlib.sha256((sig + "|v1").encode()).hexdigest()
    cached = db.get(SeedPlanCache, key)
    if cached:
        schedule = {oid: {**e, "start": _dt.fromisoformat(e["start"]), "end": _dt.fromisoformat(e["end"])}
                    for oid, e in cached.data.items()}
        status = "cache"
    else:
        pb = PlanBuilder(model, forecast(model))
        res = pb.solve_cpsat(time_limit=60, workers=8, deterministic=1.0)
        if res.status not in ("optimal", "feasible"):
            log.warning("Начальный план не оптимизирован: %s", res.status)
            return 0
        schedule, status = res.schedule, res.status
    if verify(model, schedule):
        log.warning("Начальный план не прошёл проверку — оставлен исходный")
        return 0
    if not cached:
        db.add(SeedPlanCache(key=key, created_at=utcnow(), data={
            oid: {**e, "start": e["start"].isoformat(), "end": e["end"].isoformat()} for oid, e in schedule.items()}))
    n = 0
    for oid, e in schedule.items():
        o = model.ops[oid]
        if e.get("fixed"):
            continue
        if (e["start"], e["track_id"], e.get("from_track_id"), e["resource_ids"]) !=                 (o.planned_start, o.track_id, o.from_track_id, o.resource_ids):
            n += 1
        o.planned_start, o.planned_end = e["start"], e["end"]
        o.forecast_start, o.forecast_end = e["start"], e["end"]
        o.track_id, o.from_track_id = e["track_id"], e.get("from_track_id")
        o.resource_ids, o.route_nodes = list(e["resource_ids"] or []), list(e["route_nodes"] or [])
    db.execute(delete(Reservation))
    db.flush()
    for tid, ops in model.ops_by_train.items():
        t = model.trains[tid]
        create_from_ops(db, model, tid, t.number, sorted(ops, key=lambda x: x.seq))
        dep = next((o for o in ops if o.kind == "departure"), None)
        if dep and dep.status != "done":
            t.expected_departure = dep.planned_start
    db.flush()
    log.info("Начальный план оптимизирован CP-SAT (%s), изменено операций: %d", status, n)
    return n


def demo_request(db: Session, cfg, *, number="Z-0001", wagons=40, dep_local_h=13.0, from_id="OTR",
                 wagon_kind="gondola", split_allowed=True, status="new"):
    day = local_day(cfg)
    dep = (day + timedelta(hours=dep_local_h)).astimezone(UTC)
    now = utcnow()
    r = TransferRequest(id=f"RQ-{number}", number=number, from_station_id=from_id, to_station_id=cfg["station"]["id"],
                        wagons_count=wagons, wagon_kind=wagon_kind, cargo="Уголь (демо)", priority=2,
                        split_allowed=split_allowed, desired_departure=dep, status=status, created_by="u-train",
                        created_at=now, updated_at=now)
    db.add(r)
    return r
