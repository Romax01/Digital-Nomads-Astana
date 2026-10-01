// Составы на уровне «Станция». Источник положения — сервер (pos: ломаная маршрута, голова,
// граница операции). Клиент только интерполирует голову вдоль той же ломаной и не дальше
// head_end; при потере связи движение останавливается. Каждый вагон ставится по двум
// тележкам на этой ломаной (см. geometry.ts::layoutConsist).
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { forecastOccupancy } from "../lib/forecast";
import { useStore } from "../lib/store";
import type { Topology, TrainState, ViewState } from "../lib/types";
import { blendVehicles, headNow, layoutConsist, pointAtExt, polyLen, smoothHead, type P2, type Vehicle } from "./geometry";
import { useLabelGroup, type LabelSpec } from "./labels";
import { buildBodies, buildBogie, buildWheelset, disposeAll } from "./models";
import { frameState, SCENE } from "./palette";
import { stationDims } from "./StationLevel";

const MAX_PER_KIND = 1400;
const NEAR_BOGIES = 1300; // дальше — только кузова (упрощение по расстоянию)
const NEAR_WHEELS = 650;

interface Placed { train: TrainState; path: P2[]; head: number; body: number; forecast: boolean }
/** Отрисованное состояние состава между кадрами: сглаженная голова и последняя раскладка. */
interface Smooth { sig: string; head: number; veh: Vehicle[]; from: Vehicle[] | null; t0: number }
const BLEND_MS = 450;
const sigOf = (pl: Placed) => {
  const p = pl.path, a = p[0], b = p[p.length - 1];
  return `${pl.forecast ? "f" : ""}${pl.train.pos?.op_id ?? "stand"}|${p.length}|${a[0].toFixed(0)},${a[1].toFixed(0)}|${b[0].toFixed(0)},${b[1].toFixed(0)}`;
};

/** Положения составов для текущего кадра: «Сейчас»/«История» — от сервера, «Прогноз» — стоянки по прогнозному плану. */
function placements(st: ViewState, topo: Topology, elapsed: number, forecastAt: string | null, mode: string, k: number,
  fcCache: { key: string; val: Record<string, { train_id: string; number: string }> }): Placed[] {
  const out: Placed[] = [];
  if (mode === "forecast" && forecastAt) {
    const key = `${st.meta.state_version}|${forecastAt}`;
    if (fcCache.key !== key) { fcCache.key = key; fcCache.val = forecastOccupancy(st, forecastAt); }
    const tracks = Object.fromEntries(topo.tracks.map((t) => [t.id, t]));
    for (const [tid, occ] of Object.entries(fcCache.val)) {
      const t = tracks[tid], tr = st.trains[occ.train_id];
      if (!t || !tr) continue;
      const L = polyLen(t.points);
      const body = Math.min(L, (tr.length_m ?? (t.useful_length_m ?? 600) * 0.7) * k);
      out.push({ train: tr, path: t.points, head: (L + body) / 2, body, forecast: true });
    }
    return out;
  }
  for (const tr of Object.values(st.trains)) {
    const p = tr.pos;
    if (!p || p.path.length < 2) continue;
    out.push({ train: tr, path: p.path, head: headNow(p, elapsed), body: p.body, forecast: false });
  }
  return out;
}

export function TrainsLayer({ topo, st }: { topo: Topology; st: ViewState }) {
  const select = useStore((s) => s.select);
  const mode = useStore((s) => s.mode);
  const forecastAt = useStore((s) => s.forecastAt);
  const selection = useStore((s) => s.selection);
  const { camera } = useThree();
  const d = useMemo(() => stationDims(topo), [topo]);
  const bodies = useMemo(() => buildBodies(), []);
  const bogieGeo = useMemo(() => buildBogie(), []);
  const wheelGeo = useMemo(() => buildWheelset(), []);
  const plateGeo = useMemo(() => new THREE.BoxGeometry(1, 1, 1), []);
  const keys = useMemo(() => Object.keys(bodies), [bodies]);
  const bodyRefs = useRef<Record<string, THREE.InstancedMesh | null>>({});
  const owners = useRef<Record<string, string[]>>({});
  const bogies = useRef<THREE.InstancedMesh>(null);
  const wheels = useRef<THREE.InstancedMesh>(null);
  const plates = useRef<THREE.InstancedMesh>(null);
  const plateOwner = useRef<string[]>([]);
  const fc = useRef({ key: "", val: {} as Record<string, { train_id: string; number: string }> });
  const heads = useRef<Record<string, [number, number, number]>>({});
  const smooth = useRef(new Map<string, Smooth>());
  useEffect(() => () => disposeAll([...Object.values(bodies), bogieGeo, wheelGeo, plateGeo]), [bodies, bogieGeo, wheelGeo, plateGeo]);

  const tmp = useMemo(() => ({ m: new THREE.Matrix4(), q: new THREE.Quaternion(), p: new THREE.Vector3(), s: new THREE.Vector3(), c: new THREE.Color(), up: new THREE.Vector3(0, 1, 0), t: new THREE.Vector3() }), []);

  useFrame(({ controls }, frameDt) => {
    const dt = Math.min(0.1, frameDt);
    const nowMs = performance.now();
    const { st: cur, elapsed } = frameState();
    if (!cur) return;
    const s = useStore.getState();
    const sel = s.selection?.type === "train" ? s.selection.id : null;
    const hl = new Set(s.highlight?.ids ?? []);
    const target = (controls as any)?.target as THREE.Vector3 | undefined;
    const dist = target ? camera.position.distanceTo(target) : 1000;
    const showBogies = dist < NEAR_BOGIES, showWheels = dist < NEAR_WHEELS;
    const counts: Record<string, number> = Object.fromEntries(keys.map((k) => [k, 0]));
    let nb = 0, nw = 0, np = 0;
    const { m, q, p, s: sc, c, up } = tmp;
    const yRail = d.railTop;
    const list = placements(cur, topo, elapsed, s.forecastAt, s.mode, d.k, fc.current);
    const newHeads: Record<string, [number, number, number]> = {};
    // модельных секунд в секунду реального времени (0 — пауза, история, прогноз, нет связи)
    const live = s.mode === "live" && s.conn === "online" && cur.meta.running;
    const mps = live ? cur.meta.speed : 0;
    const seen = new Set<string>();
    for (const pl of list) {
      const tr = pl.train;
      seen.add(tr.id);
      const sig = sigOf(pl);
      let sm = smooth.current.get(tr.id);
      if (!sm || sm.sig !== sig) {
        // новый маршрут: голова — по серверу, а видимые вагоны плавно перетекают из прежнего положения
        sm = { sig, head: pl.head, veh: sm?.veh ?? [], from: sm?.veh?.length ? sm.veh : null, t0: nowMs };
        smooth.current.set(tr.id, sm);
      } else if (live && pl.train.pos?.moving) {
        if (Math.abs(pl.head - sm.head) > 120) { sm.from = sm.veh; sm.t0 = nowMs; }  // крупная поправка — тоже перетеканием
        sm.head = smoothHead(sm.head, pl.head, (pl.train.pos.speed || 0) * mps, dt, pl.train.pos.head_end);
      } else {
        sm.head = pl.head;
      }
      let veh: Vehicle[] = layoutConsist(pl.path, sm.head, pl.body, tr.consist, tr.wagons, tr.kind);
      if (sm.from) {
        const a = (nowMs - sm.t0) / BLEND_MS;
        if (a >= 1 || sm.from.length !== veh.length) sm.from = null;
        else veh = blendVehicles(sm.from, veh, a);
      }
      sm.veh = veh;
      const hp = pointAtExt(pl.path, sm.head);
      newHeads[tr.id] = [hp[0], yRail + 6 * d.kc, hp[1]];
      const isSel = sel === tr.id, isHl = hl.has(tr.id);
      for (const v of veh) {
        const key = v.kind === "loco" ? "loco" : bodies[`${v.kind}${v.loaded ? "_l" : ""}`] ? `${v.kind}${v.loaded ? "_l" : ""}` : bodies[v.kind] ? v.kind : "covered";
        const mesh = bodyRefs.current[key];
        const i = counts[key];
        if (mesh && i < MAX_PER_KIND) {
          q.setFromAxisAngle(up, -v.yaw);
          m.compose(p.set(v.x, yRail, v.y), q, sc.set(v.lenU, d.kc, d.kc));
          mesh.setMatrixAt(i, m);
          c.set(v.faulty ? "#ff7a7a" : pl.forecast ? "#9ec9ff" : "#ffffff");
          mesh.setColorAt(i, c);
          (owners.current[key] ||= [])[i] = tr.id;
          counts[key] = i + 1;
        }
        if (showBogies) {
          for (const [bx, by, ba] of [[v.fx, v.fy, v.fyaw], [v.rx, v.ry, v.ryaw]]) {
            if (nb >= MAX_PER_KIND * 4) break;
            q.setFromAxisAngle(up, -ba);
            m.compose(p.set(bx, yRail, by), q, sc.set(d.kc * 0.85, d.kc, d.kc));
            bogies.current?.setMatrixAt(nb++, m);
            if (showWheels && wheels.current && nw < MAX_PER_KIND * 8) {
              for (const off of [-0.9, 0.9]) {
                const ox = Math.cos(ba) * off * d.kc * 0.85, oy = Math.sin(ba) * off * d.kc * 0.85;
                m.compose(p.set(bx + ox, yRail, by + oy), q, sc.set(d.kc, d.kc, d.kc));
                wheels.current.setMatrixAt(nw++, m);
              }
            }
          }
        }
        if ((isSel || isHl) && plates.current && np < 4000) {
          q.setFromAxisAngle(up, -v.yaw);
          m.compose(p.set(v.x, d.ballastH + 0.05, v.y), q, sc.set(v.lenU * 1.02, 0.08, d.ballastW * 1.5));
          plates.current.setMatrixAt(np, m);
          plates.current.setColorAt(np, c.set(isSel ? SCENE.select : SCENE.highlight));
          plateOwner.current[np++] = tr.id;
        }
      }
    }
    heads.current = newHeads;
    for (const id of smooth.current.keys()) if (!seen.has(id)) smooth.current.delete(id);
    for (const key of keys) {
      const mesh = bodyRefs.current[key];
      if (!mesh) continue;
      mesh.count = counts[key];
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    }
    if (bogies.current) { bogies.current.count = nb; bogies.current.instanceMatrix.needsUpdate = true; }
    if (wheels.current) { wheels.current.count = nw; wheels.current.instanceMatrix.needsUpdate = true; }
    if (plates.current) {
      plates.current.count = np; plates.current.instanceMatrix.needsUpdate = true;
      if (plates.current.instanceColor) plates.current.instanceColor.needsUpdate = true;
    }
  });

  // подписи составов: номер, число вагонов, задержка, неисправность
  const labels = useMemo<LabelSpec[]>(() => {
    const out: LabelSpec[] = [];
    const ids = mode === "forecast" ? Object.values(forecastAt ? forecastOccupancy(st, forecastAt) : {}).map((o) => o.train_id) :
      Object.values(st.trains).filter((t) => t.pos && t.pos.path.length >= 2).map((t) => t.id);
    for (const id of ids) {
      const t = st.trains[id];
      if (!t) continue;
      const selected = selection?.type === "train" && selection.id === id;
      out.push({
        id: `train:${id}`, text: `№ ${t.number}${t.faulty_wagons.length ? " ⚠" : ""}${t.delay_min >= 5 ? ` +${t.delay_min}′` : ""}`,
        sub: `${t.wagons} ваг.${mode === "forecast" ? " · прогноз" : t.pos?.moving ? ` · ${t.current_op?.label?.toLowerCase() ?? "движение"}` : t.pos?.waiting ? " · ожидает приёма" : ""}`,
        priority: selected ? 100 : t.conflict_ids.length ? 85 : t.pos?.moving ? 70 : 55,
        cls: `train k-${t.kind}${selected ? " sel" : ""}${t.conflict_ids.length ? " bad" : ""}`,
        pos: () => heads.current[id] ?? null, onClick: () => select({ type: "train", id }),
      });
    }
    return out;
  }, [st.trains, mode, forecastAt, selection, st.operations]);
  useLabelGroup("trains", labels, [labels]);

  const pick = (key: string) => (e: any) => {
    e.stopPropagation();
    const id = owners.current[key]?.[e.instanceId];
    if (id) select({ type: "train", id });
  };
  return (
    <group>
      {keys.map((key) => (
        <instancedMesh key={key} ref={(r) => { bodyRefs.current[key] = r; withColors(r); }} args={[bodies[key], undefined, MAX_PER_KIND]} castShadow
          onClick={pick(key)} frustumCulled={false}>
          <meshStandardMaterial vertexColors roughness={0.62} metalness={0.18} />
        </instancedMesh>
      ))}
      <instancedMesh ref={bogies} args={[bogieGeo, undefined, MAX_PER_KIND * 4]} frustumCulled={false}>
        <meshStandardMaterial vertexColors roughness={0.7} metalness={0.3} />
      </instancedMesh>
      <instancedMesh ref={wheels} args={[wheelGeo, undefined, MAX_PER_KIND * 8]} frustumCulled={false}>
        <meshStandardMaterial vertexColors roughness={0.5} metalness={0.6} />
      </instancedMesh>
      <instancedMesh ref={(r) => { (plates as any).current = r; withColors(r); }} args={[plateGeo, undefined, 4000]} frustumCulled={false}
        onClick={(e) => { e.stopPropagation(); const id = plateOwner.current[e.instanceId ?? -1]; if (id) select({ type: "train", id }); }}>
        <meshBasicMaterial transparent opacity={0.55} toneMapped={false} depthWrite={false} />
      </instancedMesh>
    </group>
  );
}

/** Буфер цветов экземпляров создаётся сразу: иначе шейдер, собранный без него, игнорирует цвета. */
function withColors(r: THREE.InstancedMesh | null) {
  if (r && !r.instanceColor) { r.setColorAt(0, new THREE.Color("#ffffff")); r.count = 0; }
}

/** Текущая точка головы состава (для камеры «Следовать за поездом» и «Выбранный объект»). */
export function trainHeadPoint(topo: Topology, id: string): [number, number, number] | null {
  const { st, elapsed } = frameState();
  const t = st?.trains[id];
  if (!t?.pos || t.pos.path.length < 2) return null;
  const d = stationDims(topo);
  const h = headNow(t.pos, elapsed);
  const p = pointAtExt(t.pos.path, h - t.pos.body / 2);
  return [p[0], d.railTop, p[1]];
}
