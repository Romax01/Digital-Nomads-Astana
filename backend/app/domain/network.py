"""Железнодорожная сеть: станции, перегоны, пути перегонов (расширение модели, уровень «Сеть»).

Источник — GeoJSON (см. docs/network-import.md): точки станций и линии перегонов в WGS-84
(долгота, широта). Координаты переводятся в локальную метрическую систему ENU относительно
центроида сети (равнопромежуточная проекция, радиус Земли 6 371 008,8 м). Градусы никогда не
используются как метры.

Три вида величин разделены:
  * физическая длина перегона length_m — из данных, а при отсутствии — геодезическая длина по
    исходным координатам (length_source = "geodesic");
  * геометрия визуализации — сглаженная осевая линия в метрах ENU; её длина может отличаться от
    физической, поэтому положение поезда задаётся долей пройденного пути (0…1) и переводится в
    точку на той же геометрии, по которой рисуются рельсы;
  * пути перегона — параллельные смещения одной осевой линии (двухпутный перегон — два
    самостоятельных пути со своими идентификаторами). Число путей задаётся данными.

Основная станция подробная (detail = detailed). Соседние — упрощённые (detail = simplified):
для них известны только число приёмных путей, допустимая длина, окна занятости и ограничения.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from app.domain.topology import CONFIG_DIR, load_config

EARTH_R = 6_371_008.8
TRACK_SPACING_M = 4.1         # междупутье двухпутного перегона (физическое)
VIS_TRACK_OFFSET_M = 900.0    # смещение путей на карте сети (увеличено для видимости, только визуализация)
THROAT_LEAD_M = 1500.0        # выход перегона из горловины вдоль оси станции


def haversine(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


class Projection:
    """Локальная равнопромежуточная проекция ENU: x — на восток, y — на север, метры."""

    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.k = math.cos(math.radians(lat0))

    def to_m(self, lon: float, lat: float) -> tuple[float, float]:
        return (math.radians(lon - self.lon0) * EARTH_R * self.k, math.radians(lat - self.lat0) * EARTH_R)

    def to_deg(self, x: float, y: float) -> tuple[float, float]:
        return (self.lon0 + math.degrees(x / (EARTH_R * self.k)), self.lat0 + math.degrees(y / EARTH_R))


def _catmull(points: list[tuple[float, float]], step_m: float = 400.0) -> list[list[float]]:
    """Центростремительный Catmull-Rom через все опорные точки; концы сохраняются точно."""
    if len(points) < 3:
        a, b = points[0], points[-1]
        n = max(1, int(math.dist(a, b) / step_m))
        return [[a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n] for i in range(n + 1)]
    pts = [points[0]] + points + [points[-1]]
    out = [list(points[0])]
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]

        def tj(ti, a, b):
            return ti + max(1e-6, math.dist(a, b)) ** 0.5
        t0 = 0.0
        t1 = tj(t0, p0, p1)
        t2 = tj(t1, p1, p2)
        t3 = tj(t2, p2, p3)
        n = max(2, int(math.dist(p1, p2) / step_m))
        for k in range(1, n + 1):
            t = t1 + (t2 - t1) * k / n

            def lerp(a, b, ta, tb):
                if tb - ta < 1e-9:
                    return a
                return ((tb - t) / (tb - ta) * a[0] + (t - ta) / (tb - ta) * b[0],
                        (tb - t) / (tb - ta) * a[1] + (t - ta) / (tb - ta) * b[1])
            a1, a2, a3 = lerp(p0, p1, t0, t1), lerp(p1, p2, t1, t2), lerp(p2, p3, t2, t3)
            b1, b2 = lerp(a1, a2, t0, t2), lerp(a2, a3, t1, t3)
            c = lerp(b1, b2, t1, t2)
            out.append([c[0], c[1]])
    out[-1] = list(points[-1])
    return [[round(x, 1), round(y, 1)] for x, y in out]


def offset_polyline(pts: list, d: float) -> list[list[float]]:
    """Параллельная линия на расстоянии d (слева по ходу — d > 0). Та же функция используется
    на клиенте для рельсов и поездов (lib/twin/geometry.ts::offsetPolyline)."""
    out = []
    n = len(pts)
    for i in range(n):
        a = pts[max(0, i - 1)]
        b = pts[min(n - 1, i + 1)]
        tx, ty = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(tx, ty) or 1.0
        out.append([round(pts[i][0] - ty / ln * d, 2), round(pts[i][1] + tx / ln * d, 2)])
    return out


def offset_tapered(pts: list, d: float, taper_m: float = 5000.0) -> list[list[float]]:
    """Смещение пути перегона, плавно сходящее к нулю у горловин: пути двухпутного перегона
    подходят к входной стрелке станции, а не обрываются в стороне от неё."""
    cum = [0.0]
    for i in range(len(pts) - 1):
        cum.append(cum[-1] + math.dist(pts[i], pts[i + 1]))
    total = cum[-1] or 1.0
    full = offset_polyline(pts, d)
    out = []
    for p, q, s in zip(pts, full, cum):
        u = min(1.0, min(s, total - s) / taper_m)
        k = u * u * (3 - 2 * u)  # smoothstep
        out.append([round(p[0] + (q[0] - p[0]) * k, 2), round(p[1] + (q[1] - p[1]) * k, 2)])
    return out


def polyline_len(pts: list) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


@lru_cache
def load_geojson(name: str = "network_demo") -> dict:
    with open(Path(CONFIG_DIR) / f"{name}.geojson", encoding="utf-8") as f:
        return json.load(f)


def validate_geojson(gj: dict) -> list[str]:
    """Проверка формата импорта: типы геометрий, обязательные свойства, ссылки и стороны горловин."""
    errs = []
    if gj.get("type") != "FeatureCollection":
        return ["ожидается FeatureCollection"]
    st_ids = set()
    for f in gj.get("features", []):
        p = f.get("properties") or {}
        if p.get("kind") == "station":
            if not p.get("id"):
                errs.append("станция без id")
            st_ids.add(p.get("id"))
            g = f.get("geometry") or {}
            if p.get("id") != "@main" and (g.get("type") != "Point" or not g.get("coordinates")):
                errs.append(f"станция {p.get('id')}: нужна геометрия Point [долгота, широта]")
            elif g.get("coordinates"):
                lon, lat = g["coordinates"][:2]
                if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                    errs.append(f"станция {p.get('id')}: координаты вне диапазона WGS-84")
    for f in gj.get("features", []):
        p = f.get("properties") or {}
        if p.get("kind") != "section":
            continue
        sid = p.get("id", "?")
        if (f.get("geometry") or {}).get("type") != "LineString":
            errs.append(f"перегон {sid}: нужна геометрия LineString")
        for k in ("from", "to"):
            if p.get(k) not in st_ids:
                errs.append(f"перегон {sid}: неизвестная станция {k}={p.get(k)}")
        for k in ("from_throat", "to_throat"):
            if p.get(k) not in ("west", "east"):
                errs.append(f"перегон {sid}: {k} должен быть west или east")
        if not isinstance(p.get("tracks"), int) or not 1 <= p["tracks"] <= 4:
            errs.append(f"перегон {sid}: tracks — целое 1…4")
    return errs


def build_network(main_cfg: dict, gj: dict | None = None) -> dict:
    gj = gj or load_geojson()
    errs = validate_geojson(gj)
    if errs:
        raise ValueError("Ошибки в данных сети: " + "; ".join(errs))
    main = main_cfg["station"]
    main_id = main["id"]
    geo = main.get("geo") or {"lat": 43.27, "lon": 76.94, "axis_deg": 0}
    neighbors = {n["id"]: n for n in main_cfg.get("neighbors", [])}
    L = main_cfg["layout"]
    scale = (main_cfg.get("derived") or {}).get("schema_scale_u_per_m") or 0.8
    main_half_m = (L["x_entry_east"] - L["x_entry_west"]) / 2 / scale

    raw_st = []
    for f in gj["features"]:
        p = f["properties"]
        if p.get("kind") != "station":
            continue
        if p["id"] == "@main":
            raw_st.append({"id": main_id, "name": main["name"], "detail": "detailed", "is_main": True,
                           "lon": geo["lon"], "lat": geo["lat"], "axis_deg": geo.get("axis_deg", 0),
                           "half_length_m": round(main_half_m)})
        else:
            lon, lat = f["geometry"]["coordinates"][:2]
            n = neighbors.get(p["id"], {})
            raw_st.append({"id": p["id"], "name": n.get("name", p.get("name", p["id"])), "detail": p.get("detail", "simplified"),
                           "is_main": False, "lon": lon, "lat": lat, "axis_deg": p.get("axis_deg", 0),
                           "half_length_m": p.get("half_length_m", 900),
                           "simplified": {k: n.get(k) for k in ("receiving_tracks", "max_train_length_m", "processing_min",
                                                               "locomotives_available", "accepts", "travel_min")}})
    lat0 = sum(s["lat"] for s in raw_st) / len(raw_st)
    lon0 = sum(s["lon"] for s in raw_st) / len(raw_st)
    proj = Projection(lat0, lon0)
    stations = {}
    for s in raw_st:
        x, y = proj.to_m(s["lon"], s["lat"])
        a = math.radians(s["axis_deg"])
        ux, uy = math.cos(a), math.sin(a)   # ось станции запад → восток в ENU
        hl = s["half_length_m"]
        stations[s["id"]] = {**s, "x_m": round(x, 1), "y_m": round(y, 1), "axis": [round(ux, 5), round(uy, 5)],
                             "throats": {"west": [round(x - ux * hl, 1), round(y - uy * hl, 1)],
                                         "east": [round(x + ux * hl, 1), round(y + uy * hl, 1)]}}
    sections = []
    for f in gj["features"]:
        p = f["properties"]
        if p.get("kind") != "section":
            continue
        a_id = main_id if p["from"] == "@main" else p["from"]
        b_id = main_id if p["to"] == "@main" else p["to"]
        A, B = stations[a_id], stations[b_id]
        ta, tb = A["throats"][p["from_throat"]], B["throats"][p["to_throat"]]
        sa = -1 if p["from_throat"] == "west" else 1   # выход из горловины — наружу вдоль оси
        sb = -1 if p["to_throat"] == "west" else 1
        lead_a = (ta[0] + sa * A["axis"][0] * THROAT_LEAD_M, ta[1] + sa * A["axis"][1] * THROAT_LEAD_M)
        lead_b = (tb[0] + sb * B["axis"][0] * THROAT_LEAD_M, tb[1] + sb * B["axis"][1] * THROAT_LEAD_M)
        mids = [proj.to_m(lon, lat) for lon, lat in f["geometry"]["coordinates"]]
        ctrl = [tuple(ta), lead_a] + mids + [lead_b, tuple(tb)]
        center = _catmull(ctrl)
        if p.get("length_m"):
            length_m, src = float(p["length_m"]), "data"
        else:
            ll = [(A["lon"], A["lat"])] + [tuple(c) for c in f["geometry"]["coordinates"]] + [(B["lon"], B["lat"])]
            length_m = sum(haversine(*ll[i], *ll[i + 1]) for i in range(len(ll) - 1))
            src = "geodesic"
        n = p["tracks"]
        offs = [((i - (n - 1) / 2) * VIS_TRACK_OFFSET_M) for i in range(n)]
        tracks = [{"id": f"{p['id']}/{i + 1}", "no": i + 1, "vis_offset_m": round(o, 1),
                   "direction": (("from_to" if i == 0 else "to_from") if n == 2 else "both"),
                   "points": offset_tapered(center, o) if o else center} for i, o in enumerate(offs)]
        sections.append({"id": p["id"], "name": p.get("name", p["id"]).replace("основная станция", main["name"])
                         .replace("Основная станция", main["name"]),
                         "from": a_id, "to": b_id, "from_throat": p["from_throat"], "to_throat": p["to_throat"],
                         "tracks_count": n, "length_m": round(length_m), "length_source": src,
                         "max_speed_kmh": p.get("max_speed_kmh"), "physical_spacing_m": TRACK_SPACING_M if n > 1 else None,
                         "centerline": center, "geometry_length_m": round(polyline_len(center)), "tracks": tracks,
                         "source": gj.get("properties", {}).get("source", "demo")})
    return {"source": gj.get("properties", {}).get("source", "demo"), "note": gj.get("properties", {}).get("note", ""),
            "projection": {"type": "ENU (равнопромежуточная, сфера R=6 371 008,8 м)", "lat0": round(lat0, 6),
                           "lon0": round(lon0, 6), "units": "метры, x — восток, y — север"},
            "main_station_id": main_id, "stations": list(stations.values()), "sections": sections}


def section_between(net: dict, a: str, b: str) -> dict | None:
    for s in net["sections"]:
        if {s["from"], s["to"]} == {a, b}:
            return s
    return None


_NET_CACHE: dict = {}


def network_for_station_cfg(cfg: dict) -> dict:
    """Сеть для конфигурации основной станции из БД (кеш по конфигурации и масштабу)."""
    key = (cfg.get("config_id"), (cfg.get("derived") or {}).get("schema_scale_u_per_m"))
    if key not in _NET_CACHE:
        _NET_CACHE.clear()
        _NET_CACHE[key] = build_network(cfg)
    return _NET_CACHE[key]


def track_for(section: dict, from_station: str) -> dict:
    """Путь перегона для направления движения: на двухпутном — путь своего направления."""
    fwd = section["from"] == from_station
    if section["tracks_count"] == 2:
        return section["tracks"][0 if fwd else 1]
    return section["tracks"][0]


@lru_cache
def network_for(config_name: str) -> dict:
    from app.domain.topology import build_topology
    cfg = load_config(config_name)
    cfg = {**cfg, "derived": build_topology(cfg)["geometry"]}
    return build_network(cfg)
