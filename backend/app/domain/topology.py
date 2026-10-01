"""Логическая топология станции и геометрия схемы.

Топология строится из декларативной конфигурации (парки, пути, стороны подключения),
поэтому смена размеров станции не требует изменения бизнес-логики. Горловины моделируются
«лестницами» стрелочных переводов: стрелка W<k>/E<k> соединяет путь k с соседними
стрелками. Маршрут — кратчайший путь в графе; стрелки маршрута резервируются на время
движения, что и даёт конфликты маршрутов на общих участках горловины.

Геометрия (points) — координаты схемы с плавными стрелочными переводами; рельсы, подсветка и
движение составов в 3D строятся по ней же (единая геометрия). Физические длины — отдельно, в метрах.
"""
from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"


@lru_cache
def load_config(name: str) -> dict:
    with open(CONFIG_DIR / f"{name}.json", encoding="utf-8") as f:
        return json.load(f)


def _arc(p0: tuple[float, float], a0: float, a1: float, r: float, step: float = 1.5) -> list[list[float]]:
    """Точки дуги окружности радиуса r: старт в p0 с направлением a0, поворот до a1 (ось y — вниз)."""
    n = max(2, int(abs(a1 - a0) * r / step) + 1)
    if a1 >= a0:  # угол растёт: центр справа по ходу (в координатах «y вниз»)
        cx, cy = p0[0] - r * math.sin(a0), p0[1] + r * math.cos(a0)
        pt = lambda a: [cx + r * math.sin(a), cy - r * math.cos(a)]  # noqa: E731
    else:
        cx, cy = p0[0] + r * math.sin(a0), p0[1] - r * math.cos(a0)
        pt = lambda a: [cx - r * math.sin(a), cy + r * math.cos(a)]  # noqa: E731
    return [pt(a0 + (a1 - a0) * i / (n - 1)) for i in range(n)]


def _densify(pts: list, step: float = 12.0) -> list[list[float]]:
    """Прямые участки дробятся, чтобы вагоны и шпалы опирались на равномерную геометрию."""
    out = [list(pts[0])]
    for a, b in zip(pts, pts[1:]):
        d = math.dist(a, b)
        k = max(1, int(d / step))
        for i in range(1, k + 1):
            out.append([a[0] + (b[0] - a[0]) * i / k, a[1] + (b[1] - a[1]) * i / k])
    return [[round(x, 3), round(y, 3)] for x, y in out]


def build_topology(cfg: dict) -> dict:
    """Топология и единая геометрия станции.

    Горловина — «лестница» стрелочных переводов. У каждой стрелки есть главное направление
    (у W0/E0 — вдоль главного пути, у остальных — вдоль стрелочной улицы). Все рёбра выходят из
    узла касательно этому направлению, а отклонение к парку выполняется дугой радиуса R. Поэтому
    любой маршрут, собранный из рёбер, гладкий (без изломов) в стрелочных переводах, и одна и та
    же геометрия используется для рельсов, подсветки, локомотива и каждого вагона.

    Три системы величин разделены:
      * физические длины, м — length_m рёбер и useful_length_m путей (из конфигурации);
      * координаты схемы (единицы u) — points рёбер и путей;
      * масштаб schema_scale (u/м) — общий для всей станции: прямая часть пути пропорциональна его
        полезной длине, а изображение состава — его фактической длине.
    Координаты визуализации (3D) получаются из координат схемы на клиенте линейным преобразованием.
    """
    sid = cfg["station"]["id"]
    L = cfg["layout"]
    gap = L["lane_gap"]
    th = math.radians(L.get("turnout_deg", 38))
    R = L.get("turnout_radius", 46)
    h, w = R * (1 - math.cos(th)), R * math.sin(th)       # смещение дуги стрелочного перевода
    t_th = math.tan(th)
    nodes: dict[str, dict] = {}
    tracks: list[dict] = []
    conns: list[dict] = []
    parks: list[dict] = [{"id": f"{sid}-PM", "name": "Главные пути", "kind": "main"}]

    def node(nid, kind, name, x, y, side=None):
        nodes[nid] = {"id": nid, "kind": kind, "name": name, "x": round(x, 3), "y": round(y, 3), "side": side}
        return nid

    def xy(nid):
        return (nodes[nid]["x"], nodes[nid]["y"])

    xw, xe = L["x_west"], L["x_east"]
    w_entry = node(f"{sid}-WENT", "entry", "Вход/выход (запад)", L["x_entry_west"], 0, "west")
    e_entry = node(f"{sid}-EENT", "entry", "Вход/выход (восток)", L["x_entry_east"], 0, "east")
    w0 = node(f"{sid}-W0", "switch", "Стр. W0", xw, 0, "west")
    e0 = node(f"{sid}-E0", "switch", "Стр. E0", xe, 0, "east")

    # раскладка путей по полосам и сторонам подключения
    lanes = []
    lane = 0
    for park in cfg["parks"]:
        for t in park["tracks"]:
            lane += 1
            lanes.append((lane, park, t))

    def spine_x(side: str, y_node: float) -> float:
        """x стрелки на прямой стрелочной улице (после выходной дуги у W0/E0)."""
        off = w + (y_node - h) / t_th
        return xw + off if side == "west" else xe - off

    # масштаб: прямая часть двустороннего пути должна вместить полезную длину
    straight = [(xe - xw) / cfg["main_track"]["length_m"]] if cfg["main_track"].get("length_m") else []
    for ln, park, t in lanes:
        if "west" in park["sides"] and "east" in park["sides"] and t.get("length_m"):
            yn = ln * gap - h
            span = (spine_x("east", yn) - w) - (spine_x("west", yn) + w)
            straight.append(span / t["length_m"])
    scale = round(0.94 * min(straight), 4) if straight else L.get("schema_scale", 0.8)

    edges_geom: dict[str, list] = {}

    def add_edge(eid, a, b, kind, track_id, length_m, pts):
        g = _densify(pts)
        edges_geom[eid] = g
        conns.append({"id": eid, "from_node": a, "to_node": b, "kind": kind, "track_id": track_id,
                      "length_m": float(length_m), "points": g})

    add_edge(f"{sid}-AP-W", w_entry, w0, "approach", None, L.get("approach_m", 500), [xy(w_entry), xy(w0)])
    add_edge(f"{sid}-AP-E", e0, e_entry, "approach", None, L.get("approach_m", 500), [xy(e0), xy(e_entry)])
    main = cfg["main_track"]
    main_id = f"{sid}-T{main['number']}"
    main_pts = _densify([xy(w0), xy(e0)])
    tracks.append({"id": main_id, "park_id": f"{sid}-PM", "number": main["number"],
                   "name": f"Главный путь {main['number']}", "kind": "main",
                   "useful_length_m": main["length_m"], "allowed_train_kinds": ["passenger", "freight"],
                   "from_node": w0, "to_node": e0, "zone_id": None, "points": main_pts})
    add_edge(f"{sid}-C-{main_id}", w0, e0, "track", main_id, main["length_m"], [xy(w0), xy(e0)])

    last = {"west": w0, "east": e0}
    for ln, park, t in lanes:
        pid = f"{sid}-P{park['id']}"
        if not any(p["id"] == pid for p in parks):
            parks.append({"id": pid, "name": park["name"], "kind": park["kind"]})
        y = ln * gap
        yn = y - h
        tid = f"{sid}-T{t['number']}"
        sides = park["sides"]
        useful = t.get("length_m") or 500
        pts: list = []
        if "west" in sides:
            wn = node(f"{sid}-W{ln}", "switch", f"Стр. W{ln}", spine_x("west", yn), yn, "west")
            if last["west"] == w0:   # выход со главного пути: дуга 0 → θ, затем прямая улица
                link = _arc(xy(w0), 0.0, th, R) + [list(xy(wn))]
            else:
                link = [list(xy(last["west"])), list(xy(wn))]
            add_edge(f"{sid}-L-{last['west']}-{wn}", last["west"], wn, "link", None,
                     round(poly_len(link) / scale, 1), link)
            last["west"] = wn
            pts += _arc(xy(wn), th, 0.0, R)   # ответвление в парк: дуга θ → 0
        if "east" in sides:
            en = node(f"{sid}-E{ln}", "switch", f"Стр. E{ln}", spine_x("east", yn), yn, "east")
            if last["east"] == e0:
                link = _arc(xy(e0), math.pi, math.pi - th, R) + [list(xy(en))]
            else:
                link = [list(xy(last["east"])), list(xy(en))]
            add_edge(f"{sid}-L-{last['east']}-{en}", last["east"], en, "link", None,
                     round(poly_len(link) / scale, 1), link)
            last["east"] = en
            east_arc = list(reversed(_arc(xy(en), math.pi - th, math.pi, R)))
        if "west" in sides and "east" in sides:
            pts += east_arc
        elif "west" in sides:   # тупик на востоке: прямая длиной полезной длины пути
            x_end = pts[-1][0] + useful * scale + 12
            # тупик не должен заходить на противоположную стрелочную улицу
            x_end = min(x_end, spine_x("east", yn) - 40) if last["east"] != e0 else x_end
            en = node(f"{sid}-EEND{ln}", "end", f"Упор {t['number']} (вост.)", x_end, y, "east")
            pts.append([x_end, y])
        else:                   # тупик на западе
            x_start = east_arc[0][0] - useful * scale - 12
            wn = node(f"{sid}-WEND{ln}", "end", f"Упор {t['number']} (зап.)", x_start, y, "west")
            pts = [[x_start, y]] + east_arc
        g = _densify(pts)
        tracks.append({"id": tid, "park_id": pid, "number": t["number"], "name": f"Путь {t['number']}",
                       "kind": park["kind"], "useful_length_m": t.get("length_m"),
                       "allowed_train_kinds": t.get("kinds", ["freight"]),
                       "from_node": wn, "to_node": en, "zone_id": t.get("zone"), "points": g})
        add_edge(f"{sid}-C-{tid}", wn, en, "track", tid, useful, g)

    # зоны: у тупикового конца своего пути (подписи не накладываются)
    zones = []
    for z in cfg["zones"]:
        ztracks = [t for t in tracks if t["zone_id"] == z["id"]]
        if ztracks:
            t0 = ztracks[0]
            ends = [nodes[t0["from_node"]], nodes[t0["to_node"]]]
            dead = next((n for n in ends if n["kind"] == "end"), ends[0])
            other = ends[1] if dead is ends[0] else ends[0]
            x = dead["x"] - 78 if dead["x"] < other["x"] else dead["x"] + 78
            y = sum(t["points"][len(t["points"]) // 2][1] for t in ztracks) / len(ztracks)
        elif z["kind"] == "inspection":
            x, y = (xw + xe) / 2 - 260, -gap * 1.4
        else:
            x, y = xe - 60, -gap * 1.4
        zones.append({"id": f"{sid}-Z{z['id']}", "code": z["id"], "name": z["name"], "kind": z["kind"],
                      "track_ids": [t["id"] for t in ztracks], "x": x, "y": y,
                      "params": {"operations": z.get("operations", [])}})
    for p in main.get("platforms", []):
        zones.append({"id": f"{sid}-Z{p['id']}", "code": p["id"], "name": p["name"], "kind": "platform",
                      "track_ids": [main_id], "x": (xw + xe) / 2, "y": -gap * 0.55,
                      "params": {"length_m": p["length_m"]}})
    return {"station_id": sid, "nodes": list(nodes.values()), "tracks": tracks, "connections": conns,
            "parks": parks, "zones": zones,
            "geometry": {"schema_scale_u_per_m": scale, "turnout_radius_u": R, "turnout_deg": math.degrees(th),
                         "units": "координаты схемы (u); физические длины — в метрах"}}


# ------------------------------------------------------------------ геометрия
def poly_len(pts: list) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def point_at(pts: list, s: float) -> tuple[float, float, float]:
    """Точка и направление (рад) на расстоянии s от начала ломаной."""
    if s <= 0:
        a, b = pts[0], pts[1]
        return a[0], a[1], math.atan2(b[1] - a[1], b[0] - a[0])
    acc = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        d = math.dist(a, b)
        if acc + d >= s and d > 0:
            k = (s - acc) / d
            return a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, math.atan2(b[1] - a[1], b[0] - a[0])
        acc += d
    a, b = pts[-2], pts[-1]
    return b[0], b[1], math.atan2(b[1] - a[1], b[0] - a[0])


def _merge(a: list, b: list) -> list:
    if a and b and math.dist(a[-1], b[0]) < 1e-6:
        return a + b[1:]
    return a + b


@dataclass
class Route:
    node_ids: list[str]               # все узлы пути
    switch_ids: list[str]             # стрелки маршрута (резервируются)
    through_track_ids: list[str]      # пути, по которым маршрут проходит транзитом
    points: list = field(default_factory=list)
    length_m: float = 0.0

    def as_dict(self):
        return {"switches": self.switch_ids, "through_tracks": self.through_track_ids, "length_m": self.length_m}


class Topology:
    """Граф станции, загружаемый из таблиц topology_nodes / track_connections / tracks."""

    def __init__(self, station_id: str, nodes: list[dict], tracks: list[dict], connections: list[dict],
                 scale: float = 0.8):
        self.station_id = station_id
        self.scale = scale  # единиц схемы на метр — общий для путей и составов
        self.nodes = {n["id"]: n for n in nodes}
        self.tracks = {t["id"]: t for t in tracks}
        self.edges = {c["id"]: c for c in connections}
        self.adj: dict[str, list[tuple[str, dict]]] = {}
        for c in connections:
            self.adj.setdefault(c["from_node"], []).append((c["to_node"], c))
            self.adj.setdefault(c["to_node"], []).append((c["from_node"], c))
        self.main_track_id = next((t["id"] for t in tracks if t["kind"] == "main"), None)
        self._cache: dict = {}

    def entry(self, side: str) -> str:
        return f"{self.station_id}-{'WENT' if side == 'west' else 'EENT'}"

    def track_switch_ends(self, track_id: str) -> list[str]:
        t = self.tracks[track_id]
        return [n for n in (t["from_node"], t["to_node"]) if self.nodes[n]["kind"] == "switch"]

    def track_end_on_side(self, track_id: str, side: str) -> str | None:
        t = self.tracks[track_id]
        n = t["from_node"] if side == "west" else t["to_node"]
        return n if self.nodes[n]["kind"] == "switch" else None

    def _dijkstra(self, src: str, targets: set[str], allowed_tracks: set[str]):
        dist = {src: 0.0}
        prev: dict[str, tuple[str, dict]] = {}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if u in targets:
                path_edges = []
                while u != src:
                    p, e = prev[u]
                    path_edges.append((p, u, e))
                    u = p
                return list(reversed(path_edges))
            if d > dist.get(u, math.inf):
                continue
            for v, e in self.adj.get(u, []):
                if e["kind"] == "track" and e["track_id"] not in allowed_tracks:
                    continue
                w = e["length_m"] + (2000 if e["kind"] == "track" else 0)
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = (u, e)
                    heapq.heappush(pq, (nd, v))
        return None

    def _route(self, src: str, targets: set[str]) -> Route | None:
        allowed = {self.main_track_id} if self.main_track_id else set()
        path = self._dijkstra(src, targets, allowed)
        if path is None:
            return None
        node_ids = [src] + [v for _, v, _ in path]
        pts: list = [[self.nodes[src]["x"], self.nodes[src]["y"]]]
        through = []
        length = 0.0
        for u, v, e in path:
            seg = e["points"] if e["from_node"] == u else list(reversed(e["points"]))
            pts = _merge(pts, [list(p) for p in seg])
            length += e["length_m"]
            if e["kind"] == "track":
                through.append(e["track_id"])
        switches = [n for n in node_ids if self.nodes[n]["kind"] == "switch"]
        return Route(node_ids, switches, through, pts, length)

    # --- маршруты движения
    def arrival_route(self, side: str, track_id: str) -> Route | None:
        key = ("arr", side, track_id)
        if key not in self._cache:
            if track_id == self.main_track_id:
                r = self._route(self.entry(side), {self.track_end_on_side(track_id, side)})
            else:
                ends = set(self.track_switch_ends(track_id))
                r = self._route(self.entry(side), ends) if ends else None
            self._cache[key] = r
        return self._cache[key]

    def departure_route(self, side: str, track_id: str) -> Route | None:
        key = ("dep", side, track_id)
        if key not in self._cache:
            r = self.arrival_route(side, track_id)
            self._cache[key] = None if r is None else Route(
                list(reversed(r.node_ids)), list(reversed(r.switch_ids)), r.through_track_ids,
                list(reversed(r.points)), r.length_m)
        return self._cache[key]

    def shunting_route(self, from_track: str, to_track: str) -> Route | None:
        key = ("sh", from_track, to_track)
        if key not in self._cache:
            best = None
            for a in self.track_switch_ends(from_track):
                r = self._route(a, set(self.track_switch_ends(to_track)))
                if r and (best is None or r.length_m < best.length_m):
                    best = r
            self._cache[key] = best
        return self._cache[key]

    # --- геометрия поезда
    def track_poly(self, track_id: str, toward_east: bool = True) -> list:
        pts = [list(p) for p in self.tracks[track_id]["points"]]
        return pts if toward_east else list(reversed(pts))

    def body_len(self, track_id: str | None, length_m: float | None) -> float:
        """Длина изображения состава в единицах схемы = фактическая длина × масштаб станции.
        Длина не подгоняется под путь; если длина неизвестна — 70 % полезной длины (с пометкой в карточке)."""
        if length_m is None:
            ul = (self.tracks[track_id]["useful_length_m"] if track_id else None) or 600
            length_m = ul * 0.7
        return max(8.0, length_m * self.scale)

    def standing_path(self, track_id: str, length_m: float | None, toward_east: bool = True):
        pts = self.track_poly(track_id, toward_east)
        total = poly_len(pts)
        body = self.body_len(track_id, length_m)
        return pts, total / 2 + body / 2, body

    def movement_path(self, kind: str, side: str | None, from_track: str | None, to_track: str | None,
                      length_m: float | None):
        """Ломаная движения и путь головы: начало (s0) и конец (s1)."""
        if kind == "arrival" and to_track:
            r = self.arrival_route(side, to_track)
            if not r:
                return None
            toward_east = side == "west"
            tpts = self.track_poly(to_track, toward_east)
            body = self.body_len(to_track, length_m)
            pts = _merge(r.points, tpts) if math.dist(r.points[-1], tpts[0]) < 1e-6 else _merge(r.points, list(reversed(tpts)))
            s1 = r_len(r.points) + poly_len(tpts) / 2 + body / 2
            return pts, 0.0, s1, body
        if kind == "departure" and from_track:
            r = self.departure_route(side, from_track)
            if not r:
                return None
            toward_east = side == "east"
            tpts = self.track_poly(from_track, toward_east)
            body = self.body_len(from_track, length_m)
            if math.dist(tpts[-1], r.points[0]) >= 1e-6:
                tpts = list(reversed(tpts))
            pts = _merge(tpts, r.points)
            s0 = poly_len(tpts) / 2 + body / 2
            return pts, s0, poly_len(pts) + body, body
        if kind == "shunting" and from_track and to_track:
            r = self.shunting_route(from_track, to_track)
            if not r:
                return None
            a = self.track_poly(from_track)
            if math.dist(a[-1], r.points[0]) >= 1e-6:
                a = list(reversed(a))
            b = self.track_poly(to_track)
            if math.dist(b[0], r.points[-1]) >= 1e-6:
                b = list(reversed(b))
            body = self.body_len(to_track, length_m)
            pts = _merge(_merge(a, r.points), b)
            s0 = poly_len(a) / 2 + self.body_len(from_track, length_m) / 2
            s1 = poly_len(a) + r_len(r.points) + poly_len(b) / 2 + body / 2
            return pts, s0, s1, body
        return None


def r_len(pts):
    return poly_len(pts)
