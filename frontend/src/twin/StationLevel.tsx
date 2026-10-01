// Уровень «Станция»: физическое изображение путей по единой геометрии backend.
// Координаты сцены: x = x схемы, z = y схемы, y — вверх. Длины — в масштабе станции
// (schema_scale_u_per_m); поперечные размеры (колея, балласт, ширина вагонов) увеличены
// в CROSS раз для читаемости и не используются ни в каких расчётах.
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { forecastOccupancy } from "../lib/forecast";
import { fmtHM } from "../lib/format";
import { TRACK_STATUS } from "../lib/labels";
import { useStore } from "../lib/store";
import type { NetworkStatic, Topology, ViewState } from "../lib/types";
import { pointAtExt, polyLen, sampleAlong, type P2 } from "./geometry";
import { useLabelGroup, type LabelSpec } from "./labels";
import { ballastGeometry, disposeAll, railsGeometry, ribbon } from "./models";
import { SCENE, STATUS_COLOR, trackDisplay } from "./palette";

export const CROSS = 2.2;
export const STUB_U = 320; // подход к перегону за входной горловиной

export interface StationDims { k: number; kc: number; gauge: number; ballastW: number; ballastH: number; sleeperH: number; railH: number; railTop: number }

export function stationDims(topo: Topology): StationDims {
  const k = topo.geometry?.schema_scale_u_per_m ?? 0.8;
  const kc = k * CROSS;
  const ballastH = 0.35 * kc, sleeperH = 0.2 * kc, railH = 0.17 * kc;
  return { k, kc, gauge: 1.52 * kc, ballastW: 4.4 * kc, ballastH, sleeperH, railH, railTop: ballastH + sleeperH + railH };
}

/** Ломаные всех путей и соединений (рёбра «track» совпадают с путями и не дублируются). */
function allPolys(topo: Topology): P2[][] {
  return [...topo.tracks.map((t) => t.points), ...topo.connections.filter((c) => c.kind !== "track").map((c) => c.points)];
}

function stubs(topo: Topology): { side: "west" | "east"; pts: P2[] }[] {
  const w = topo.nodes.find((n) => n.kind === "entry" && n.side === "west");
  const e = topo.nodes.find((n) => n.kind === "entry" && n.side === "east");
  const out: { side: "west" | "east"; pts: P2[] }[] = [];
  if (w) out.push({ side: "west", pts: [[w.x - STUB_U, w.y], [w.x, w.y]] });
  if (e) out.push({ side: "east", pts: [[e.x, e.y], [e.x + STUB_U, e.y]] });
  return out;
}

/** Статичная часть пути: общий балласт, рельсы и шпалы (одна геометрия / один InstancedMesh). */
function TrackBed({ topo, d }: { topo: Topology; d: StationDims }) {
  const sleepers = useRef<THREE.InstancedMesh>(null);
  const { ballast, rails, samples } = useMemo(() => {
    const polys = [...allPolys(topo), ...stubs(topo).map((s) => s.pts)];
    const step = Math.max(1.2, 1.25 * d.kc);
    const samples = polys.flatMap((p) => sampleAlong(p, step));
    return {
      ballast: ballastGeometry(polys, d.ballastW, d.ballastH),
      rails: railsGeometry(polys, d.gauge, Math.max(0.16, 0.09 * d.kc), d.railH, d.ballastH + d.sleeperH),
      samples,
    };
  }, [topo, d]);
  const sleeperGeo = useMemo(() => new THREE.BoxGeometry(1, 1, 1), []);
  useEffect(() => {
    const m = sleepers.current;
    if (!m) return;
    const mat = new THREE.Matrix4(), q = new THREE.Quaternion(), up = new THREE.Vector3(0, 1, 0);
    const sc = new THREE.Vector3(0.26 * d.kc, d.sleeperH, 2.75 * d.kc);
    samples.forEach(([x, y, a], i) => {
      q.setFromAxisAngle(up, -a);
      mat.compose(new THREE.Vector3(x, d.ballastH + d.sleeperH / 2, y), q, sc);
      m.setMatrixAt(i, mat);
    });
    m.count = samples.length;
    m.instanceMatrix.needsUpdate = true;
    m.computeBoundingSphere();
  }, [samples, d]);
  useEffect(() => () => disposeAll([ballast, rails, sleeperGeo]), [ballast, rails, sleeperGeo]);
  return (
    <group>
      <mesh geometry={ballast} receiveShadow><meshStandardMaterial color={SCENE.ballast} roughness={0.98} /></mesh>
      <instancedMesh ref={sleepers} args={[sleeperGeo, undefined, Math.max(1, samples.length)]} receiveShadow>
        <meshStandardMaterial color={SCENE.sleeper} roughness={0.9} />
      </instancedMesh>
      <mesh geometry={rails} castShadow><meshStandardMaterial color={SCENE.rail} metalness={0.6} roughness={0.35} emissive="#2b5a70" emissiveIntensity={0.35} /></mesh>
    </group>
  );
}

/** Путь: кликабельная подложка, полосы статуса по краям балласта, выделение и подсветка. */
function TrackItem({ t, d, status, selected, highlighted, conflict, onSelect }: {
  t: Topology["tracks"][number]; d: StationDims; status: string; selected: boolean; highlighted: boolean; conflict: boolean; onSelect: () => void;
}) {
  const geo = useMemo(() => {
    const off = d.ballastW / 2 + 0.35 * d.kc;
    const w = Math.max(1.2, 0.7 * d.kc);
    const parts = [ribbon(t.points, w, 0.02, d.ballastH * 0.6, off), ribbon(t.points, w, 0.02, d.ballastH * 0.6, -off)];
    const strip = new THREE.BufferGeometry();
    const pos = [...parts[0].attributes.position.array, ...parts[1].attributes.position.array];
    strip.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    strip.computeVertexNormals();
    disposeAll(parts);
    return { strip, hit: ribbon(t.points, d.ballastW * 2.2, 0.05, 0), glow: ribbon(t.points, d.ballastW * 2.6, 0.03, d.ballastH * 0.4) };
  }, [t, d]);
  useEffect(() => () => disposeAll([geo.strip, geo.hit, geo.glow]), [geo]);
  const glowMat = useRef<THREE.MeshBasicMaterial>(null);
  useFrame(({ clock }) => {
    if (glowMat.current) glowMat.current.opacity = conflict && !selected ? 0.18 + 0.14 * Math.sin(clock.elapsedTime * 3.2) : selected ? 0.28 : 0.22;
  });
  const color = STATUS_COLOR[status] ?? STATUS_COLOR.unknown;
  return (
    <group>
      <mesh geometry={geo.hit} onClick={(e) => { e.stopPropagation(); onSelect(); }} visible={false} />
      <mesh geometry={geo.strip}>
        <meshBasicMaterial color={color} transparent opacity={status === "unknown" ? 0.55 : 0.95} toneMapped={false} />
      </mesh>
      {(selected || highlighted || conflict) && (
        <mesh geometry={geo.glow}>
          <meshBasicMaterial ref={glowMat} color={selected ? SCENE.select : highlighted ? SCENE.highlight : SCENE.bad} transparent depthWrite={false} toneMapped={false} />
        </mesh>
      )}
    </group>
  );
}

function Switch({ n, d, state, selected, onSelect }: { n: Topology["nodes"][number]; d: StationDims; state: any; selected: boolean; onSelect: () => void }) {
  const lamp = state?.closed ? SCENE.bad : state?.route_op ? SCENE.accent : state?.position ? "#7d8ea0" : "#b892ff";
  const z = n.y - (d.ballastW / 2 + 1.4 * d.kc);
  return (
    <group position={[n.x, 0, z]} onClick={(e) => { e.stopPropagation(); onSelect(); }}>
      <mesh position={[0, 0.5 * d.kc, 0]} castShadow>
        <boxGeometry args={[1.6 * d.kc, 1.0 * d.kc, 0.9 * d.kc]} />
        <meshStandardMaterial color="#59636e" metalness={0.4} roughness={0.5} />
      </mesh>
      <mesh position={[0, 1.25 * d.kc, 0]}>
        <sphereGeometry args={[0.38 * d.kc, 12, 10]} />
        <meshBasicMaterial color={lamp} toneMapped={false} />
      </mesh>
      {selected && (
        <mesh position={[0, 0.1, 0]} rotation={[-Math.PI / 2, 0, 0]}>
          <ringGeometry args={[2.2 * d.kc, 2.8 * d.kc, 32]} />
          <meshBasicMaterial color={SCENE.select} transparent opacity={0.85} side={THREE.DoubleSide} toneMapped={false} />
        </mesh>
      )}
    </group>
  );
}

function BufferStop({ n, d, dir }: { n: Topology["nodes"][number]; d: StationDims; dir: number }) {
  return (
    <group position={[n.x + dir * 0.6 * d.kc, d.ballastH, n.y]}>
      <mesh position={[0, 0.7 * d.kc, 0]} castShadow>
        <boxGeometry args={[0.6 * d.kc, 1.4 * d.kc, d.gauge * 1.6]} />
        <meshStandardMaterial color="#c7c9cc" />
      </mesh>
      <mesh position={[-dir * 0.32 * d.kc, 0.95 * d.kc, 0]}>
        <boxGeometry args={[0.08 * d.kc, 0.35 * d.kc, d.gauge * 1.62]} />
        <meshStandardMaterial color="#b3302a" />
      </mesh>
      <mesh position={[0, 1.7 * d.kc, 0]}>
        <sphereGeometry args={[0.25 * d.kc, 10, 8]} />
        <meshBasicMaterial color={SCENE.bad} toneMapped={false} />
      </mesh>
    </group>
  );
}

/** Сооружения по зонам конфигурации: только то, что есть в данных станции. */
function Zones({ topo, d, selectedId, onSelect }: { topo: Topology; d: StationDims; selectedId?: string; onSelect: (id: string) => void }) {
  const byId = useMemo(() => Object.fromEntries(topo.tracks.map((t) => [t.id, t])), [topo]);
  const nodes = useMemo(() => Object.fromEntries(topo.nodes.map((n) => [n.id, n])), [topo]);
  return (
    <group>
      {topo.zones.map((z) => {
        const sel = selectedId === z.id;
        const click = (e: any) => { e.stopPropagation(); onSelect(z.id); };
        const mat = (c: string) => <meshStandardMaterial color={c} roughness={0.7} emissive={sel ? SCENE.select : "#000"} emissiveIntensity={sel ? 0.25 : 0} />;
        if (z.kind === "platform") {
          const len = (z.params?.length_m ?? 300) * d.k;
          return (
            <group key={z.id} onClick={click}>
              <mesh position={[z.x, 0.55 * d.kc, z.y]} receiveShadow castShadow><boxGeometry args={[len, 1.1 * d.kc, 6 * d.kc]} />{mat("#55606b")}</mesh>
              <mesh position={[z.x, 1.12 * d.kc, z.y + 2.7 * d.kc]}><boxGeometry args={[len, 0.04 * d.kc, 0.3 * d.kc]} /><meshBasicMaterial color="#e6c229" /></mesh>
              {/* вокзал за платформой */}
              <mesh position={[z.x, 5 * d.kc, z.y - 13 * d.kc]} castShadow><boxGeometry args={[Math.min(len * 0.45, 70 * d.kc), 10 * d.kc, 12 * d.kc]} />{mat("#3b4a5a")}</mesh>
              <mesh position={[z.x, 6.5 * d.kc, z.y - 6.9 * d.kc]}><boxGeometry args={[Math.min(len * 0.42, 66 * d.kc), 2.2 * d.kc, 0.1 * d.kc]} /><meshBasicMaterial color="#9fd8ff" transparent opacity={0.55} /></mesh>
              <mesh position={[z.x, 10.4 * d.kc, z.y - 13 * d.kc]}><boxGeometry args={[Math.min(len * 0.47, 72 * d.kc), 0.8 * d.kc, 13 * d.kc]} />{mat("#2a3440")}</mesh>
            </group>
          );
        }
        const tr = z.track_ids.map((id) => byId[id]).filter(Boolean);
        if ((z.kind === "repair" || z.kind === "cargo_front") && tr.length) {
          // сооружение над тупиковым концом пути (по данным топологии)
          return (
            <group key={z.id} onClick={click}>
              {tr.map((t) => {
                const a = nodes[t.from_node], b = nodes[t.to_node];
                const deadEast = b?.kind === "end";
                const L = polyLen(t.points);
                const shedLen = Math.min(L * 0.45, (z.kind === "repair" ? 120 : 90) * d.k * 1.4);
                const s = deadEast ? L - shedLen / 2 - 4 : shedLen / 2 + 4;
                const [x, y] = pointAtExt(t.points, s);
                const w = 9 * d.kc, h = z.kind === "repair" ? 9 * d.kc : 7 * d.kc;
                void a;
                return z.kind === "repair" ? (
                  <group key={t.id} position={[x, 0, y]}>
                    <mesh position={[0, h / 2, w / 2]} castShadow><boxGeometry args={[shedLen, h, 0.4 * d.kc]} />{mat("#4a3b44")}</mesh>
                    <mesh position={[0, h / 2, -w / 2]} castShadow><boxGeometry args={[shedLen, h, 0.4 * d.kc]} />{mat("#4a3b44")}</mesh>
                    <mesh position={[0, h + 0.3 * d.kc, 0]} castShadow><boxGeometry args={[shedLen, 0.6 * d.kc, w + 1.2 * d.kc]} />{mat("#5a4652")}</mesh>
                  </group>
                ) : (
                  <group key={t.id} position={[x, 0, y]}>
                    {/* склад рядом с путём и козловой кран над путём */}
                    <mesh position={[0, h / 2, -(w / 2 + 5 * d.kc)]} castShadow><boxGeometry args={[shedLen, h, w]} />{mat("#5a5130")}</mesh>
                    {[-1, 1].map((sx) => (
                      <group key={sx}>
                        <mesh position={[sx * shedLen * 0.18, 5 * d.kc, 4.8 * d.kc]}><boxGeometry args={[0.5 * d.kc, 10 * d.kc, 0.5 * d.kc]} /><meshStandardMaterial color={SCENE.warn} /></mesh>
                        <mesh position={[sx * shedLen * 0.18, 5 * d.kc, -4.8 * d.kc]}><boxGeometry args={[0.5 * d.kc, 10 * d.kc, 0.5 * d.kc]} /><meshStandardMaterial color={SCENE.warn} /></mesh>
                      </group>
                    ))}
                    <mesh position={[0, 10.2 * d.kc, 0]}><boxGeometry args={[shedLen * 0.42, 0.7 * d.kc, 10.4 * d.kc]} /><meshStandardMaterial color={SCENE.warn} /></mesh>
                  </group>
                );
              })}
            </group>
          );
        }
        const size = z.kind === "loco_depot" ? [36, 8, 14] : z.kind === "inspection" ? [12, 5, 8] : [20, 6, 10];
        return (
          <group key={z.id} position={[z.x, 0, z.y]} onClick={click}>
            <mesh position={[0, size[1] * d.kc / 2, 0]} castShadow><boxGeometry args={[size[0] * d.kc, size[1] * d.kc, size[2] * d.kc]} />{mat(z.kind === "loco_depot" ? "#34475a" : "#2f4152")}</mesh>
            <mesh position={[0, size[1] * d.kc + 0.3 * d.kc, 0]}><boxGeometry args={[size[0] * d.kc + 0.8, 0.6 * d.kc, size[2] * d.kc + 0.8]} />{mat("#26313c")}</mesh>
          </group>
        );
      })}
    </group>
  );
}

/** Датчики: столбик с индикатором качества данных (цвет + подпись в карточке). */
function Devices({ topo, st, d, selectedId, onSelect }: { topo: Topology; st: ViewState; d: StationDims; selectedId?: string; onSelect: (id: string) => void }) {
  const alertDev = useMemo(() => new Set(Object.values(st.alerts).map((a) => a.device_id).filter(Boolean)), [st.alerts]);
  return (
    <group>
      {topo.devices.filter((dv) => dv.x != null && dv.y != null).map((dv) => {
        const ts = st.tracks[dv.object_id];
        const ds = ts?.data_state;
        const c = alertDev.has(dv.id) ? SCENE.warn : ds === "actual" ? SCENE.ok : ds === "contradictory" ? STATUS_COLOR.contradictory : ds ? STATUS_COLOR.unknown : "#7d8ea0";
        const sel = selectedId === dv.id;
        return (
          <group key={dv.id} position={[dv.x, 0, dv.y + d.ballastW / 2 + 2.2 * d.kc]} onClick={(e) => { e.stopPropagation(); onSelect(dv.id); }}>
            <mesh position={[0, 1.1 * d.kc, 0]}><cylinderGeometry args={[0.12 * d.kc, 0.12 * d.kc, 2.2 * d.kc, 6]} /><meshStandardMaterial color="#59636e" /></mesh>
            <mesh position={[0, 2.35 * d.kc, 0]}><boxGeometry args={[0.6 * d.kc, 0.5 * d.kc, 0.4 * d.kc]} /><meshBasicMaterial color={c} toneMapped={false} /></mesh>
            {sel && <mesh position={[0, 0.05, 0]} rotation={[-Math.PI / 2, 0, 0]}><ringGeometry args={[1.2 * d.kc, 1.6 * d.kc, 24]} /><meshBasicMaterial color={SCENE.select} side={THREE.DoubleSide} toneMapped={false} /></mesh>}
          </group>
        );
      })}
    </group>
  );
}

export function StationLevel({ topo, st, net }: { topo: Topology; st: ViewState; net: NetworkStatic | null }) {
  const select = useStore((s) => s.select);
  const selection = useStore((s) => s.selection);
  const highlight = useStore((s) => s.highlight);
  const d = useMemo(() => stationDims(topo), [topo]);
  const hl = useMemo(() => new Set(highlight?.ids ?? []), [highlight]);
  const mode = useStore((s) => s.mode);
  const forecastAt = useStore((s) => s.forecastAt);
  const nowMs = new Date(st.meta.model_time).getTime();
  const selId = selection?.id;
  // «Прогноз»: занятость путей — по прогнозному плану на выбранный момент (не смешивается с наблюдением)
  const fc = useMemo(() => (mode === "forecast" && forecastAt ? forecastOccupancy(st, forecastAt) : null), [mode, forecastAt, st.operations]);
  const statusOf = (tid: string) => {
    const ts = st.tracks[tid];
    if (!fc) return trackDisplay(ts, nowMs);
    if (ts?.status === "closed" && ts.closure?.until && forecastAt && new Date(ts.closure.until) > new Date(forecastAt)) return "closed";
    return fc[tid] ? "occupied" : "free";
  };
  const sid = topo.station.id;

  // подписи: пути (номер + статус), стрелки рядом, зоны, подходы к перегонам
  const labels = useMemo<LabelSpec[]>(() => {
    const out: LabelSpec[] = [];
    for (const t of topo.tracks) {
      const ts = st.tracks[t.id];
      const ds = statusOf(t.id);
      const info = TRACK_STATUS[ds];
      // у начала прямой части пути (за дугой стрелочного перевода): начала путей разнесены «лестницей»
      const [x, y] = pointAtExt(t.points, Math.min(polyLen(t.points) * 0.3, 70));
      const conflict = (ts?.conflict_ids?.length ?? 0) > 0;
      const occ = fc ? fc[t.id]?.number : ts?.occupant_number;
      const sub = ds === "reserved" && ts?.next_reservation ? `${info.icon} ${info.label} с ${fmtHM(ts.next_reservation.start)}` :
        ds !== "free" ? `${info.icon} ${info.label}${fc && ds === "occupied" ? " по прогнозу" : ""}${occ && ds === "occupied" ? ` · № ${occ}` : ""}` : undefined;
      out.push({
        id: `trk:${t.id}`, text: `${t.number}${conflict ? " ⚠" : ""}`, sub, priority: selId === t.id ? 95 : conflict ? 75 : ds === "free" ? 30 : 50,
        cls: `st-${ds}${selId === t.id ? " sel" : ""}${hl.has(t.id) ? " hl" : ""}`, pos: () => [x, 4 * d.kc, y - 1.5 * d.kc],
        onClick: () => select({ type: "track", id: t.id }),
      });
    }
    for (const z of topo.zones) {
      out.push({ id: `zone:${z.id}`, text: z.name, priority: selId === z.id ? 90 : 18, cls: "zone", maxDist: 2600,
        pos: () => [z.x, (z.kind === "platform" ? 12 : 11) * d.kc, z.kind === "platform" ? z.y - 13 * d.kc : z.y], onClick: () => select({ type: "zone", id: z.id }) });
    }
    for (const n of topo.nodes) {
      if (n.kind !== "switch") continue;
      out.push({ id: `sw:${n.id}`, text: n.name.replace("Стр. ", "стр. "), priority: selId === n.id ? 90 : 8, cls: "small", maxDist: 900,
        pos: () => [n.x, 2.6 * d.kc, n.y - (d.ballastW / 2 + 1.4 * d.kc)], onClick: () => select({ type: "switch", id: n.id }) });
    }
    for (const s of stubs(topo)) {
      const sec = net?.sections.find((q) => (q.from === sid && q.from_throat === s.side) || (q.to === sid && q.to_throat === s.side));
      const other = sec ? net!.stations.find((q) => q.id === (sec.from === sid ? sec.to : sec.from)) : null;
      const p = s.side === "west" ? s.pts[0] : s.pts[1];
      out.push({ id: `stub:${s.side}`, text: other ? `${s.side === "west" ? "←" : "→"} ${other.name}` : `${s.side === "west" ? "←" : "→"} перегон`,
        sub: sec ? `${sec.tracks_count === 2 ? "двухпутный" : "однопутный"} перегон, ${(sec.length_m / 1000).toFixed(0)} км` : "нет данных сети",
        priority: 60, cls: "stub", pos: () => [p[0] + (s.side === "west" ? 40 : -40), 5 * d.kc, p[1] - 6 * d.kc],
        onClick: sec ? () => select({ type: "section", id: sec.id }) : undefined });
    }
    return out;
  }, [topo, st.tracks, nowMs, selId, hl, net, d, fc]);
  useLabelGroup("station", labels, [labels]);

  return (
    <group>
      <TrackBed topo={topo} d={d} />
      {topo.tracks.map((t) => {
        const ts = st.tracks[t.id];
        return (
          <TrackItem key={t.id} t={t} d={d} status={statusOf(t.id)} selected={selection?.type === "track" && selId === t.id}
            highlighted={hl.has(t.id)} conflict={(ts?.conflict_ids?.length ?? 0) > 0} onSelect={() => select({ type: "track", id: t.id })} />
        );
      })}
      {topo.nodes.filter((n) => n.kind === "switch").map((n) => (
        <Switch key={n.id} n={n} d={d} state={st.switches[n.id]} selected={selection?.type === "switch" && selId === n.id}
          onSelect={() => select({ type: "switch", id: n.id })} />
      ))}
      {topo.nodes.filter((n) => n.kind === "end").map((n) => <BufferStop key={n.id} n={n} d={d} dir={n.side === "east" ? 1 : -1} />)}
      <Zones topo={topo} d={d} selectedId={selection?.type === "zone" ? selId : undefined} onSelect={(id) => select({ type: "zone", id })} />
      <Devices topo={topo} st={st} d={d} selectedId={selection?.type === "device" ? selId : undefined} onSelect={(id) => select({ type: "device", id })} />
    </group>
  );
}
