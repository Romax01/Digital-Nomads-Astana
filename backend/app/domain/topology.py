"""Логическая топология станции и геометрия схемы.

Топология строится из декларативной конфигурации (парки, пути, стороны подключения),
поэтому смена размеров станции не требует изменения бизнес-логики. Горловины моделируются
«лестницами» стрелочных переводов: стрелка W<k>/E<k> соединяет путь k с соседними
стрелками. Маршрут — кратчайший путь в графе; стрелки маршрута резервируются на время
движения, что и даёт конфликты маршрутов на общих участках горловины.

Геометрия (points) — схематичная (не в масштабе); 2D и 3D используют одни и те же
идентификаторы и координаты.
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


def build_topology(cfg: dict) -> dict:
    sid = cfg["station"]["id"]
    L = cfg["layout"]
    gap = L["lane_gap"]
    nodes: dict[str, dict] = {}
    tracks: list[dict] = []
    conns: list[dict] = []
    parks: list[dict] = [{"id": f"{sid}-PM", "name": "Главные пути", "kind": "main"}]

    def node(nid, kind, name, x, y, side=None):
        nodes[nid] = {"id": nid, "kind": kind, "name": name, "x": x, "y": y, "side": side}
        return nid

    w_entry = node(f"{sid}-WENT", "entry", "Вход/выход (запад)", L["x_entry_west"], 0, "west")
    e_entry = node(f"{sid}-EENT", "entry", "Вход/выход (восток)", L["x_entry_east"], 0, "east")
    w0 = node(f"{sid}-W0", "switch", "Стр. W0", L["x_west"], 0, "west")
    e0 = node(f"{sid}-E0", "switch", "Стр. E0", L["x_east"], 0, "east")
    conns.append(_edge(f"{sid}-AP-W", w_entry, w0, "approach", None, 500, nodes))
    conns.append(_edge(f"{sid}-AP-E", e0, e_entry, "approach", None, 500, nodes))

    main = cfg["main_track"]
    main_id = f"{sid}-T{main['number']}"
    tracks.append({"id": main_id, "park_id": f"{sid}-PM", "number": main["number"],
                   "name": f"Главный путь {main['number']}", "kind": "main",
                   "useful_length_m": main["length_m"], "allowed_train_kinds": ["passenger", "freight"],
                   "from_node": w0, "to_node": e0, "zone_id": None,
                   "points": [[nodes[w0]["x"], 0], [nodes[e0]["x"], 0]]})
    conns.append(_edge(f"{sid}-C-{main_id}", w0, e0, "track", main_id, main["length_m"], nodes))

    lane = 0
    last_w, last_e = w0, e0
    for park in cfg["parks"]:
        pid = f"{sid}-P{park['id']}"
        parks.append({"id": pid, "name": park["name"], "kind": park["kind"]})
        for t in park["tracks"]:
            lane += 1
            y = lane * gap
            tid = f"{sid}-T{t['number']}"
            sides = park["sides"]
            if "west" in sides:
                wn = node(f"{sid}-W{lane}", "switch", f"Стр. W{lane}", L["x_west"] + L["dx"] * lane, y, "west")
                conns.append(_edge(f"{sid}-L-{last_w}-{wn}", last_w, wn, "link", None, 60, nodes))
                last_w = wn
            else:
                wn = node(f"{sid}-WEND{lane}", "end", f"Упор {t['number']} (зап.)", L["x_east_start"], y, "west")
            if "east" in sides:
                en = node(f"{sid}-E{lane}", "switch", f"Стр. E{lane}", L["x_east"] - L["dx"] * lane, y, "east")
                conns.append(_edge(f"{sid}-L-{last_e}-{en}", last_e, en, "link", None, 60, nodes))
                last_e = en
            else:
                en = node(f"{sid}-EEND{lane}", "end", f"Упор {t['number']} (вост.)", L["x_west_end"], y, "east")
            tracks.append({"id": tid, "park_id": pid, "number": t["number"], "name": f"Путь {t['number']}",
                           "kind": park["kind"], "useful_length_m": t.get("length_m"),
                           "allowed_train_kinds": t.get("kinds", ["freight"]),
                           "from_node": wn, "to_node": en, "zone_id": t.get("zone"),
                           "points": [[nodes[wn]["x"], y], [nodes[en]["x"], y]]})
            conns.append(_edge(f"{sid}-C-{tid}", wn, en, "track", tid, t.get("length_m") or 500, nodes))

    # зоны: координаты — центр связанных путей (или условная точка у горловины)
    zones = []
    for z in cfg["zones"]:
        ztracks = [t for t in tracks if t["zone_id"] == z["id"]]
        if ztracks:
            # подпись — у тупикового конца пути, на его уровне: соседние фронты не накладываются
            t0 = ztracks[0]
            ends = [nodes[t0["from_node"]], nodes[t0["to_node"]]]
            dead = next((n for n in ends if n["kind"] == "end"), ends[0])
            other = ends[1] if dead is ends[0] else ends[0]
            x = dead["x"] - 78 if dead["x"] < other["x"] else dead["x"] + 78
            y = sum(t["points"][0][1] for t in ztracks) / len(ztracks)
        elif z["kind"] == "inspection":
            x, y = (L["x_west"] + L["x_east"]) / 2 - 260, -gap * 1.4
        else:
            x, y = L["x_east"] - 60, -gap * 1.4
        zones.append({"id": f"{sid}-Z{z['id']}", "code": z["id"], "name": z["name"], "kind": z["kind"],
                      "track_ids": [t["id"] for t in ztracks], "x": x, "y": y,
                      "params": {"operations": z.get("operations", [])}})
    for p in main.get("platforms", []):
        zones.append({"id": f"{sid}-Z{p['id']}", "code": p["id"], "name": p["name"], "kind": "platform",
                      "track_ids": [main_id], "x": (L["x_west"] + L["x_east"]) / 2, "y": -gap * 0.55,
                      "params": {"length_m": p["length_m"]}})
    return {"station_id": sid, "nodes": list(nodes.values()), "tracks": tracks,
            "connections": conns, "parks": parks, "zones": zones}


def _edge(eid, a, b, kind, track_id, length, nodes):
    return {"id": eid, "from_node": a, "to_node": b, "kind": kind, "track_id": track_id,
            "length_m": float(length),
            "points": [[nodes[a]["x"], nodes[a]["y"]], [nodes[b]["x"], nodes[b]["y"]]]}


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

    def __init__(self, station_id: str, nodes: list[dict], tracks: list[dict], connections: list[dict]):
        self.station_id = station_id
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
        """Длина изображения состава в единицах схемы: доля полезной длины пути."""
        if not track_id:
            return 120.0
        t = self.tracks[track_id]
        draw = poly_len(t["points"])
        ul = t["useful_length_m"] or 800
        frac = min(0.95, (length_m or ul * 0.7) / ul)
        return max(30.0, draw * frac)

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
