"""Единая геометрия станции и модель сети: гладкость маршрутов, масштаб составов, проекция, фазы поездов."""
import math
from datetime import timedelta

import pytest

from app.core.timeutil import aware
from app.db import SessionLocal
from app.domain.network import Projection, build_network, haversine, load_geojson, validate_geojson
from app.domain.topology import Topology, build_topology, load_config, poly_len
from app.models import SimState
from tests.conftest import fresh_observations


def _topo(name):
    cfg = load_config(name)
    t = build_topology(cfg)
    return cfg, t, Topology(cfg["station"]["id"], t["nodes"], t["tracks"], t["connections"],
                            t["geometry"]["schema_scale_u_per_m"])


def _max_kink_deg(pts):
    worst = 0.0
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        if math.dist(a, b) < 0.5 or math.dist(b, c) < 0.5:
            continue
        h1 = math.atan2(b[1] - a[1], b[0] - a[0])
        h2 = math.atan2(c[1] - b[1], c[0] - b[0])
        d = abs((h2 - h1 + math.pi) % (2 * math.pi) - math.pi)
        worst = max(worst, math.degrees(d))
    return worst


@pytest.mark.parametrize("name", ["large", "small"])
def test_routes_are_smooth_through_turnouts(name):
    """Маршруты приёма и отправления на каждый путь — без изломов (стрелки — дуги, касательные к узлам)."""
    cfg, t, topo = _topo(name)
    checked = 0
    nodes = {n["id"]: n for n in t["nodes"]}
    for tr in t["tracks"]:
        for side in ("west", "east"):
            end = tr["from_node"] if side == "west" else tr["to_node"]
            if nodes[end]["kind"] != "switch":
                continue  # тупик с этой стороны: маршрут со сменой направления — излом допустим
            for kind in ("arrival", "departure"):
                args = (side, None, tr["id"]) if kind == "arrival" else (side, tr["id"], None)
                mp = topo.movement_path(kind, *args, 500)
                if not mp:
                    continue
                assert _max_kink_deg(mp[0]) < 6.0, (tr["id"], side, kind)
                checked += 1
    assert checked >= len(t["tracks"])


@pytest.mark.parametrize("name", ["large", "small"])
def test_train_body_uses_one_scale(name):
    """Изображение состава = фактическая длина × единый масштаб станции; длина не подгоняется под путь."""
    cfg, t, topo = _topo(name)
    sc = t["geometry"]["schema_scale_u_per_m"]
    for tr in t["tracks"]:
        assert topo.body_len(tr["id"], 590) == pytest.approx(590 * sc)
    # прямая часть двустороннего пути вмещает полезную длину в том же масштабе
    for tr in t["tracks"]:
        nodes = {n["id"]: n for n in t["nodes"]}
        if nodes[tr["from_node"]]["kind"] == "switch" and nodes[tr["to_node"]]["kind"] == "switch":
            assert poly_len(tr["points"]) >= tr["useful_length_m"] * sc


def test_small_station_keeps_its_topology():
    """Небольшая станция не получает сортировочный парк, депо и лишние пути."""
    cfg, t, _ = _topo("small")
    kinds = {tr["kind"] for tr in t["tracks"]}
    assert "sorting" not in kinds and "depot" not in kinds and "repair" not in kinds
    assert len(t["tracks"]) == 1 + sum(len(p["tracks"]) for p in cfg["parks"])


def test_projection_matches_geodesic_distance():
    pr = Projection(43.5, 76.4)
    for (lo1, la1), (lo2, la2) in [((76.0, 43.3), (76.9, 43.7)), ((75.2, 43.5), (77.1, 43.7))]:
        a, b = pr.to_m(lo1, la1), pr.to_m(lo2, la2)
        geo = haversine(lo1, la1, lo2, la2)
        assert abs(math.dist(a, b) - geo) / geo < 0.01
    x, y = pr.to_m(76.7, 43.6)
    lo, la = pr.to_deg(x, y)
    assert lo == pytest.approx(76.7) and la == pytest.approx(43.6)


@pytest.mark.parametrize("name", ["large", "small"])
def test_sections_start_and_end_at_throats(name):
    cfg = load_config(name)
    cfg = {**cfg, "derived": build_topology(cfg)["geometry"]}
    net = build_network(cfg)
    st = {s["id"]: s for s in net["stations"]}
    assert net["source"] == "demo"
    assert st[cfg["station"]["id"]]["detail"] == "detailed"
    assert all(s["detail"] == "simplified" for s in net["stations"] if not s["is_main"])
    for sec in net["sections"]:
        a = st[sec["from"]]["throats"][sec["from_throat"]]
        b = st[sec["to"]]["throats"][sec["to_throat"]]
        assert math.dist(sec["centerline"][0], a) < 1 and math.dist(sec["centerline"][-1], b) < 1
        assert len(sec["tracks"]) == sec["tracks_count"]  # второй путь не добавляется сам
        for tr in sec["tracks"]:  # каждый путь перегона подходит к горловине
            assert math.dist(tr["points"][0], a) < 1 and math.dist(tr["points"][-1], b) < 1
        assert sec["length_source"] in ("data", "geodesic") and sec["length_m"] > 0
        # длина визуализации — отдельная величина, но не может быть «случайной»
        assert 0.8 < sec["geometry_length_m"] / sec["length_m"] < 1.25


def test_geojson_validation_reports_errors():
    gj = load_geojson()
    assert validate_geojson(gj) == []
    bad = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [200, 10]}, "properties": {"kind": "station", "id": "X"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [10, 10]}, "properties": {"kind": "section", "id": "S"}}]}
    errs = validate_geojson(bad)
    assert any("WGS-84" in e for e in errs) and any("LineString" in e for e in errs)


def test_train_is_never_on_section_and_station_at_once(world):
    world("normal")
    from app.services.hub import Hub
    from app.sim.engine import Engine
    hub = Hub()
    eng = Engine()
    seen_phases = set()
    for _ in range(10):
        with SessionLocal() as db:
            s = db.get(SimState, 1)
            s.model_time = aware(s.model_time) + timedelta(minutes=6)
            db.commit()
            fresh_observations(db)
        eng.step(advance=False)
        hub.step(False)
        st = hub.state
        net = st["network"]
        assert net and net["source"] == "demo"
        for tid, ph in net["trains"].items():
            seen_phases.add(ph["phase"])
            tr = st["trains"][tid]
            if ph["phase"] in ("on_section", "at_origin", "arrived"):
                assert tr["pos"] is None or tr["pos"].get("waiting"), (tid, ph, tr["status"])
            if ph["phase"] == "on_section":
                assert 0 <= ph["frac"] <= ph["frac_max"]
        for tid, tr in st["trains"].items():
            if tr["pos"] and not tr["pos"].get("waiting"):
                assert net["trains"].get(tid, {}).get("phase") in (None, "at_station"), (tid, tr["status"], net["trains"].get(tid))
            c = tr["consist"]
            if c["source"] == "wagons":
                assert sum(g["count"] for g in c["groups"]) == tr["wagons"]
    assert "on_section" in seen_phases


def test_network_endpoint(client, world, auth):
    world("normal")
    r = client.get("/api/v1/network", headers=auth("duty"))
    assert r.status_code == 200
    body = r.json()
    assert body["projection"]["units"].startswith("метры")
    assert {s["id"] for s in body["stations"]} >= {"OTR", "ZHT"}
    assert client.get("/api/v1/network").status_code == 401
