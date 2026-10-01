// Уровень «Железнодорожная сеть». Геометрия — метры ENU от backend (/api/v1/network),
// в сцене 1 единица = NET_M метров; север — к верхнему краю экрана (z = −y).
// Положение поезда на перегоне — доля пути (0…1) по геометрии того же пути перегона,
// по которой нарисованы рельсы; интерполяция не дальше frac_max.
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { useStore } from "../lib/store";
import type { NetworkStatic, NetTrain, Topology, ViewState } from "../lib/types";
import { cumulative, fracNow, pointAtExt, type P2 } from "./geometry";
import { useLabelGroup, type LabelSpec } from "./labels";
import { ballastGeometry, disposeAll, railsGeometry, ribbon } from "./models";
import { frameState, KIND_COLOR, SCENE } from "./palette";

export const NET_M = 40; // метров в единице сцены уровня «Сеть»

const NET_TRACK_W = 18; // условная ширина пути перегона на уровне «Сеть», ед.

export const enu = (p: number[]): P2 => [p[0] / NET_M, -p[1] / NET_M];

export function networkBounds(net: NetworkStatic) {
  const pts = [...net.stations.map((s) => enu([s.x_m, s.y_m])), ...net.sections.flatMap((s) => s.centerline.map(enu))];
  const xs = pts.map((p) => p[0]), zs = pts.map((p) => p[1]);
  return { min_x: Math.min(...xs), max_x: Math.max(...xs), min_z: Math.min(...zs), max_z: Math.max(...zs) };
}

/** Схема основной станции в метрах, повёрнутая по оси станции: та же геометрия путей, что на уровне «Станция». */
function mainStationPolys(topo: Topology, st: NetworkStatic["stations"][number]): P2[][] {
  const k = topo.geometry?.schema_scale_u_per_m ?? 0.8;
  const xm = ((topo.layout?.x_entry_west ?? topo.bounds.min_x) + (topo.layout?.x_entry_east ?? topo.bounds.max_x)) / 2;
  const [ux, uy] = st.axis;
  const tr = (p: number[]): P2 => {
    const along = (p[0] - xm) / k, lat = p[1] / k; // lat — к «югу» от главного пути (ось y схемы вниз)
    const x = st.x_m + ux * along + uy * lat, y = st.y_m + uy * along - ux * lat;
    return enu([x, y]);
  };
  return [...topo.tracks.map((t) => t.points.map(tr)), ...topo.connections.filter((c) => c.kind !== "track").map((c) => c.points.map(tr))];
}

interface TrackRuntime { id: string; section: string; pts: P2[]; cum: number[] }

/** Точка поезда сети для текущего кадра (камера «Следовать», подписи). */
export function netTrainPoint(net: NetworkStatic, tracks: Record<string, TrackRuntime>, ph: NetTrain, elapsed: number): [number, number, number] | null {
  if (ph.phase === "on_section" || ph.phase === "waiting_entry") {
    const t = ph.track_id ? tracks[ph.track_id] : undefined;
    if (!t) return null;
    const L = t.cum[t.cum.length - 1];
    const f = fracNow(ph as any, elapsed);
    const s = ph.reverse ? (1 - f) * L : f * L;
    const p = pointAtExt(t.pts, s, t.cum);
    return [p[0], 14, p[1]];
  }
  const sid = ph.station_id;
  const st = net.stations.find((s) => s.id === sid);
  if (!st) return null;
  const [x, z] = enu([st.x_m, st.y_m]);
  return [x, 4, z];
}

export function useNetTracks(net: NetworkStatic | null) {
  return useMemo(() => {
    const out: Record<string, TrackRuntime> = {};
    if (!net) return out;
    for (const s of net.sections) for (const t of s.tracks) { const pts = t.points.map(enu); out[t.id] = { id: t.id, section: s.id, pts, cum: cumulative(pts) }; }
    return out;
  }, [net]);
}

function Section({ sec, selected, onSelect }: { sec: NetworkStatic["sections"][number]; selected: boolean; onSelect: () => void }) {
  const geo = useMemo(() => {
    const polys = sec.tracks.map((t) => t.points.map(enu));
    return {
      // ширина пути на карте сети условная (в масштабе 1:40 путь был бы тоньше пикселя)
      ballast: ballastGeometry(polys, NET_TRACK_W, 0.6),
      rails: railsGeometry(polys, NET_TRACK_W * 0.45, NET_TRACK_W * 0.12, 0.5, 0.6),
      hit: ribbon(sec.centerline.map(enu), 60 + (sec.tracks_count - 1) * 24, 0.05, 0),
      glow: ribbon(sec.centerline.map(enu), 34 + (sec.tracks_count - 1) * 24, 0.05, 0.1),
    };
  }, [sec]);
  useEffect(() => () => disposeAll(Object.values(geo)), [geo]);
  return (
    <group>
      <mesh geometry={geo.hit} visible={false} onClick={(e) => { e.stopPropagation(); onSelect(); }} />
      {selected && <mesh geometry={geo.glow}><meshBasicMaterial color={SCENE.select} transparent opacity={0.22} depthWrite={false} toneMapped={false} /></mesh>}
      <mesh geometry={geo.ballast} receiveShadow><meshStandardMaterial color="#33485a" roughness={0.9} emissive="#0e2a3a" emissiveIntensity={0.8} /></mesh>
      <mesh geometry={geo.rails}><meshStandardMaterial color={SCENE.rail} metalness={0.5} roughness={0.4} emissive="#3fb6e0" emissiveIntensity={0.7} /></mesh>
    </group>
  );
}

function MainStation({ topo, st, selected, onSelect, summary }: { topo: Topology | null; st: NetworkStatic["stations"][number]; selected: boolean; onSelect: () => void; summary: any }) {
  const geo = useMemo(() => {
    if (!topo) return null;
    const polys = mainStationPolys(topo, st);
    return { lines: ballastGeometry(polys, 2.2, 0.8) };
  }, [topo, st]);
  useEffect(() => () => { if (geo) disposeAll([geo.lines]); }, [geo]);
  const [x, z] = enu([st.x_m, st.y_m]);
  const len = (st.half_length_m * 2) / NET_M;
  const ang = Math.atan2(-st.axis[1], st.axis[0]);
  const ring = useRef<THREE.Mesh>(null);
  useFrame(({ clock }) => { if (ring.current) { const k = 1 + 0.06 * Math.sin(clock.elapsedTime * 2); ring.current.scale.set(k, k, k); } });
  const alarm = (summary?.critical ?? 0) > 0;
  return (
    <group onClick={(e) => { e.stopPropagation(); onSelect(); }}>
      <mesh position={[x, 0.15, z]} rotation={[0, -ang, 0]} receiveShadow>
        <boxGeometry args={[len * 1.15, 0.3, Math.max(40, len * 0.55)]} />
        <meshStandardMaterial color="#0c2230" emissive="#1c85a8" emissiveIntensity={selected ? 0.6 : 0.28} transparent opacity={0.9} />
      </mesh>
      {geo && <mesh geometry={geo.lines}><meshBasicMaterial color={SCENE.accent} toneMapped={false} /></mesh>}
      <mesh ref={ring} position={[x, 0.4, z]} rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[Math.max(len * 0.7, 60), Math.max(len * 0.7, 60) + 5, 64]} />
        <meshBasicMaterial color={selected ? SCENE.select : alarm ? SCENE.bad : "#2d7999"} transparent opacity={0.9} side={THREE.DoubleSide} toneMapped={false} />
      </mesh>
    </group>
  );
}

function NeighborStation({ st, selected, onSelect, restricted }: { st: NetworkStatic["stations"][number]; selected: boolean; onSelect: () => void; restricted: boolean }) {
  const [x, z] = enu([st.x_m, st.y_m]);
  const len = (st.half_length_m * 2) / NET_M;
  const ang = Math.atan2(-st.axis[1], st.axis[0]);
  const n = st.simplified?.receiving_tracks ?? 2;
  return (
    <group onClick={(e) => { e.stopPropagation(); onSelect(); }}>
      {/* условное изображение: число приёмных путей — из данных, раскладка путей неизвестна */}
      <group position={[x, 0, z]} rotation={[0, -ang, 0]}>
        {Array.from({ length: n }, (_, i) => (
          <mesh key={i} position={[0, 0.4, (i - (n - 1) / 2) * 7]}>
            <boxGeometry args={[len * 0.8, 0.6, 3]} />
            <meshStandardMaterial color="#3e5566" />
          </mesh>
        ))}
      </group>
      <mesh position={[x, 4.2, z]}>
        <cylinderGeometry args={[14, 14, 8, 32]} />
        <meshStandardMaterial color="#63d8ff" emissive="#1c85a8" emissiveIntensity={selected ? 0.9 : 0.45} transparent opacity={0.55} />
      </mesh>
      <mesh position={[x, 0.3, z]} rotation={[-Math.PI / 2, 0, 0]}>
        <ringGeometry args={[Math.max(len * 0.6, 40), Math.max(len * 0.6, 40) + 4, 48]} />
        <meshBasicMaterial color={selected ? SCENE.select : restricted ? SCENE.warn : "#2d7999"} side={THREE.DoubleSide} toneMapped={false} />
      </mesh>
    </group>
  );
}

/** Поезда на перегонах: символ дальнего масштаба (вытянутый блок по направлению пути) с номером и числом вагонов. */
function NetTrains({ net, tracks }: { net: NetworkStatic; tracks: Record<string, TrackRuntime> }) {
  const mesh = useRef<THREE.InstancedMesh>(null);
  const owner = useRef<string[]>([]);
  const select = useStore((s) => s.select);
  const tmp = useMemo(() => ({ m: new THREE.Matrix4(), q: new THREE.Quaternion(), p: new THREE.Vector3(), s: new THREE.Vector3(), c: new THREE.Color(), up: new THREE.Vector3(0, 1, 0) }), []);
  const geo = useMemo(() => new THREE.BoxGeometry(1, 1, 1), []);
  useEffect(() => () => geo.dispose(), [geo]);
  useFrame(() => {
    const { st, elapsed } = frameState();
    const m = mesh.current;
    if (!m || !st?.network) return;
    const s = useStore.getState();
    const sel = s.selection?.type === "train" ? s.selection.id : null;
    const hl = new Set(s.highlight?.ids ?? []);
    let i = 0;
    for (const ph of Object.values(st.network.trains)) {
      if (ph.phase !== "on_section" && ph.phase !== "waiting_entry") continue;
      const t = ph.track_id ? tracks[ph.track_id] : undefined;
      if (!t) continue;
      const L = t.cum[t.cum.length - 1];
      const f = fracNow(ph as any, elapsed);
      const sAt = ph.reverse ? (1 - f) * L : f * L;
      const p = pointAtExt(t.pts, sAt, t.cum);
      const yaw = p[2] + (ph.reverse ? Math.PI : 0);
      const len = Math.max(70, (ph.length_m ?? 600) / NET_M); // условный символ дальнего масштаба
      tmp.q.setFromAxisAngle(tmp.up, -yaw);
      // символ стоит позади головы: голова — в точке доли пути
      tmp.m.compose(tmp.p.set(p[0] - Math.cos(yaw) * len / 2, 4, p[1] - Math.sin(yaw) * len / 2), tmp.q, tmp.s.set(len, 10, 16));
      m.setMatrixAt(i, tmp.m);
      m.setColorAt(i, tmp.c.set(sel === ph.train_id ? SCENE.select : hl.has(ph.train_id) ? SCENE.highlight : KIND_COLOR[ph.kind] ?? "#c9d4df"));
      owner.current[i++] = ph.train_id;
    }
    m.count = i;
    m.instanceMatrix.needsUpdate = true;
    if (m.instanceColor) m.instanceColor.needsUpdate = true;
    m.computeBoundingSphere();
  });
  void net;
  return (
    <instancedMesh ref={(r) => { (mesh as any).current = r; if (r && !r.instanceColor) { r.setColorAt(0, new THREE.Color("#fff")); r.count = 0; } }}
      args={[geo, undefined, 400]} frustumCulled={false}
      onClick={(e) => { e.stopPropagation(); const id = owner.current[e.instanceId ?? -1]; if (id) select({ type: "train", id }); }}>
      <meshStandardMaterial emissive="#0b2a38" emissiveIntensity={0.6} />
    </instancedMesh>
  );
}

export function NetworkLevel({ net, topo, st }: { net: NetworkStatic; topo: Topology | null; st: ViewState }) {
  const select = useStore((s) => s.select);
  const selection = useStore((s) => s.selection);
  const tracks = useNetTracks(net);
  const live = st.network;
  const selId = selection?.id;

  const labels = useMemo<LabelSpec[]>(() => {
    const out: LabelSpec[] = [];
    for (const s of net.stations) {
      const [x, z] = enu([s.x_m, s.y_m]);
      const sum = live?.stations[s.id];
      const sub = s.is_main
        ? `Подробная модель · поездов: ${sum?.trains_here ?? "—"} · конфликтов: ${sum?.conflicts ?? "—"}`
        : `Упрощённая модель · приёмных путей: ${s.simplified?.receiving_tracks ?? "?"}${sum?.inbound ? ` · на подходе: ${sum.inbound}` : ""}${sum?.restriction ? " · ⚠ ограничение" : ""}`;
      out.push({ id: `st:${s.id}`, text: s.name, sub, priority: selId === s.id ? 100 : s.is_main ? 90 : 80,
        cls: `station${selId === s.id ? " sel" : ""}${sum?.restriction || (sum?.critical ?? 0) > 0 ? " warn" : ""}`,
        pos: () => [x, 10, z - (s.half_length_m / NET_M) * 0.7], onClick: () => select({ type: "station", id: s.id }) });
    }
    for (const sec of net.sections) {
      const c = sec.centerline;
      const mid = enu(c[Math.floor(c.length / 2)]);
      out.push({ id: `sec:${sec.id}`, text: sec.name, sub: `${sec.tracks_count === 2 ? "двухпутный" : "однопутный"} · ${(sec.length_m / 1000).toFixed(1)} км${sec.length_source === "geodesic" ? " (по координатам)" : ""}`,
        priority: selId === sec.id ? 95 : 40, cls: `section${selId === sec.id ? " sel" : ""}`, pos: () => [mid[0], 4, mid[1]],
        onClick: () => select({ type: "section", id: sec.id }) });
    }
    if (live) {
      for (const ph of Object.values(live.trains)) {
        if (ph.phase !== "on_section" && ph.phase !== "waiting_entry") continue;
        const to = net.stations.find((s) => s.id === ph.to);
        out.push({ id: `nt:${ph.train_id}`, text: `№ ${ph.number}${ph.delay_min >= 5 ? ` +${ph.delay_min}′` : ""}`,
          sub: `${ph.wagons} ваг. → ${to?.name ?? ph.to}${ph.phase === "waiting_entry" ? " · ожидает приёма" : ""}`,
          priority: selId === ph.train_id ? 99 : 60, cls: `train k-${ph.kind}${selId === ph.train_id ? " sel" : ""}`,
          pos: () => { const { elapsed } = frameState(); const cur = frameState().st?.network?.trains[ph.train_id] ?? ph; return netTrainPoint(net, tracks, cur, elapsed); },
          onClick: () => select({ type: "train", id: ph.train_id }) });
      }
    }
    return out;
  }, [net, live, selId, tracks]);
  useLabelGroup("network", labels, [labels]);

  return (
    // уровень сети приподнят над землёй: на дальних дистанциях иначе возникает конфликт глубины с сеткой
    <group position={[0, 6, 0]}>
      {net.sections.map((s) => <Section key={s.id} sec={s} selected={selection?.type === "section" && selId === s.id} onSelect={() => select({ type: "section", id: s.id })} />)}
      {net.stations.map((s) => s.is_main
        ? <MainStation key={s.id} topo={topo} st={s} selected={selId === s.id} summary={live?.stations[s.id]} onSelect={() => select({ type: "station", id: s.id })} />
        : <NeighborStation key={s.id} st={s} selected={selId === s.id} restricted={!!live?.stations[s.id]?.restriction} onSelect={() => select({ type: "station", id: s.id })} />)}
      {live && <NetTrains net={net} tracks={tracks} />}
    </group>
  );
}
