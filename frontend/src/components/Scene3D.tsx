// 3D-двойник. Источник состояния — backend: сцена только отображает занятость и позиции,
// рассчитанные моделью, и не определяет занятость самостоятельно. Интерполяция между
// обновлениями идёт только вдоль разрешённого маршрута и не дальше конечной точки операции.
import { Html, OrbitControls } from "@react-three/drei";
import { Canvas, useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { headNow, pointAt, polyLen } from "../lib/geom";
import { TRACK_STATUS } from "../lib/labels";
import { useStore } from "../lib/store";
import type { Topology, ViewState } from "../lib/types";

const COLORS: Record<string, string> = {
  free: "#5d6f82", occupied: "#4f9dff", unknown: "#b892ff", contradictory: "#ff9f43", closed: "#ff5c5c",
};
const TRAIN_COLORS: Record<string, string> = { freight: "#c9d4df", transfer: "#7fd0ff", passenger: "#f5cf5a" };
const box = new THREE.BoxGeometry(1, 1, 1);
const MAX_WAGONS = 2400;

function Segment({ a, b, color, width = 3, height = 1.2, y = 0, opacity = 1, onClick }: any) {
  const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
  const ang = Math.atan2(b[1] - a[1], b[0] - a[0]);
  return (
    <mesh geometry={box} position={[(a[0] + b[0]) / 2, y + height / 2, (a[1] + b[1]) / 2]} rotation={[0, -ang, 0]}
      scale={[len, height, width]} onClick={onClick}>
      <meshStandardMaterial color={color} transparent={opacity < 1} opacity={opacity} />
    </mesh>
  );
}

function Tracks({ topo, st, onSelect, selectedId }: { topo: Topology; st: ViewState; onSelect: (id: string) => void; selectedId?: string }) {
  return (
    <group>
      {topo.connections.filter((c) => c.kind !== "track").map((c) => (
        <Segment key={c.id} a={c.points[0]} b={c.points[c.points.length - 1]} color="#3d4b5a" width={2} height={0.6} />
      ))}
      {topo.tracks.map((t) => {
        const ts = st.tracks[t.id];
        const status = ts?.status ?? "unknown";
        const sel = selectedId === t.id;
        const mid = pointAt(t.points, polyLen(t.points) / 2);
        const info = TRACK_STATUS[status];
        const click = (e: any) => { e.stopPropagation(); onSelect(t.id); };
        return (
          <group key={t.id}>
            {t.points.slice(0, -1).map((p, i) => (
              <Segment key={i} a={p} b={t.points[i + 1]} color={sel ? "#ffd166" : COLORS[status]} width={sel ? 5 : 3.2}
                opacity={status === "unknown" ? 0.55 : 1} onClick={click} />
            ))}
            <Html position={[t.points[0][0] + 8, 4, t.points[0][1]]} center style={{ pointerEvents: "none" }}>
              <span style={{ fontSize: 11, fontWeight: 700, color: "#e7edf3", textShadow: "0 0 3px #000" }}>{t.number}</span>
            </Html>
            {status !== "free" && status !== "occupied" && (
              <Html position={[mid[0], 10, mid[1]]} center style={{ pointerEvents: "none" }}>
                <span style={{ fontSize: 11, fontWeight: 700, color: COLORS[status], background: "rgba(13,20,28,.85)", padding: "1px 5px", borderRadius: 4, whiteSpace: "nowrap" }}>
                  {info.icon} {info.label}
                </span>
              </Html>
            )}
            {(ts?.conflict_ids?.length ?? 0) > 0 && <ConflictRing x={mid[0]} z={mid[1]} />}
          </group>
        );
      })}
    </group>
  );
}

function ConflictRing({ x, z }: { x: number; z: number }) {
  const ref = useRef<THREE.Mesh>(null);
  useFrame(({ clock }) => {
    if (ref.current) { const k = 1 + 0.25 * Math.sin(clock.elapsedTime * 3); ref.current.scale.set(k, k, k); }
  });
  return (
    <mesh ref={ref} position={[x, 3, z]} rotation={[-Math.PI / 2, 0, 0]}>
      <ringGeometry args={[10, 13, 32]} />
      <meshBasicMaterial color="#ff5c5c" transparent opacity={0.8} side={THREE.DoubleSide} />
    </mesh>
  );
}

/** Вагоны всех составов — один InstancedMesh (повторное использование геометрии). */
function Trains({ onSelect }: { onSelect: (id: string) => void }) {
  const wagons = useRef<THREE.InstancedMesh>(null);
  const locos = useRef<THREE.InstancedMesh>(null);
  const owner = useRef<string[]>([]);
  const locoOwner = useRef<string[]>([]);
  const m = useMemo(() => new THREE.Matrix4(), []);
  const q = useMemo(() => new THREE.Quaternion(), []);
  const col = useMemo(() => new THREE.Color(), []);
  const up = useMemo(() => new THREE.Vector3(0, 1, 0), []);
  useFrame(() => {
    const s = useStore.getState();
    const st = s.mode === "history" ? s.replay.frames[s.replay.index]?.state : s.live;
    if (!st || !wagons.current || !locos.current) return;
    const elapsed = s.mode === "live" && st.meta.running ? ((Date.now() - s.lastMsgAt) / 1000) * st.meta.speed : 0;
    let i = 0, j = 0;
    owner.current = []; locoOwner.current = [];
    for (const t of Object.values(st.trains)) {
      if (!t.pos || t.pos.path.length < 2 || s.mode === "forecast") continue;
      const head = headNow(t.pos, elapsed);
      const n = Math.max(1, Math.min(40, t.wagons || 1));
      const step = t.pos.body / (n + 1);
      const sel = s.selection?.type === "train" && s.selection.id === t.id;
      // локомотив
      const lp = pointAt(t.pos.path, head - step / 2);
      q.setFromAxisAngle(up, -lp[2]);
      m.compose(new THREE.Vector3(lp[0], 4, lp[1]), q, new THREE.Vector3(step * 0.92, 7, 7));
      locos.current.setMatrixAt(j, m);
      locos.current.setColorAt(j, col.set(sel ? "#ffd166" : "#ff8f5c"));
      locoOwner.current[j++] = t.id;
      for (let k = 1; k <= n && i < MAX_WAGONS; k++) {
        const sp = head - step * (k + 0.5);
        if (sp < 0) break;
        const p = pointAt(t.pos.path, sp);
        q.setFromAxisAngle(up, -p[2]);
        m.compose(new THREE.Vector3(p[0], 3.2, p[1]), q, new THREE.Vector3(step * 0.86, 5.6, 6));
        wagons.current.setMatrixAt(i, m);
        const faulty = t.faulty_wagons.length > 0 && k === Math.ceil(n / 2);
        wagons.current.setColorAt(i, col.set(sel ? "#ffe39a" : faulty ? "#ff5c5c" : TRAIN_COLORS[t.kind] ?? "#c9d4df"));
        owner.current[i++] = t.id;
      }
    }
    wagons.current.count = i; locos.current.count = j;
    wagons.current.instanceMatrix.needsUpdate = true; locos.current.instanceMatrix.needsUpdate = true;
    if (wagons.current.instanceColor) wagons.current.instanceColor.needsUpdate = true;
    if (locos.current.instanceColor) locos.current.instanceColor.needsUpdate = true;
  });
  return (
    <group>
      <instancedMesh ref={wagons} args={[box, undefined, MAX_WAGONS]} onClick={(e) => { e.stopPropagation(); if (e.instanceId !== undefined) onSelect(owner.current[e.instanceId]); }}>
        <meshStandardMaterial />
      </instancedMesh>
      <instancedMesh ref={locos} args={[box, undefined, 200]} onClick={(e) => { e.stopPropagation(); if (e.instanceId !== undefined) onSelect(locoOwner.current[e.instanceId]); }}>
        <meshStandardMaterial />
      </instancedMesh>
    </group>
  );
}

function TrainLabels({ st }: { st: ViewState }) {
  return (
    <>
      {Object.values(st.trains).map((t) => {
        if (!t.pos || t.pos.path.length < 2) return null;
        const p = pointAt(t.pos.path, t.pos.head_s);
        return (
          <Html key={t.id} position={[p[0], 16, p[1]]} center style={{ pointerEvents: "none" }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: "#fff", background: "rgba(20,30,40,.8)", padding: "0 4px", borderRadius: 3, whiteSpace: "nowrap" }}>
              {t.number}{t.delay_min >= 5 ? ` +${t.delay_min}′` : ""}{t.faulty_wagons.length ? " ⚠" : ""}
            </span>
          </Html>
        );
      })}
    </>
  );
}

function Zones({ topo, onSelect }: { topo: Topology; onSelect: (id: string) => void }) {
  return (
    <>
      {topo.zones.map((z) => (
        <group key={z.id} position={[z.kind === "cargo_front" || z.kind === "repair" ? z.x - 115 : z.x, 0, z.y]} onClick={(e) => { e.stopPropagation(); onSelect(z.id); }}>
          <mesh geometry={box} position={[0, 2, 0]} scale={[120, 4, 14]}>
            <meshStandardMaterial color={z.kind === "repair" ? "#5a3b47" : z.kind === "cargo_front" ? "#5a5130" : z.kind === "platform" ? "#3c4a5a" : "#2f4152"} />
          </mesh>
          <Html position={[0, 12, 0]} center style={{ pointerEvents: "none" }}>
            <span style={{ fontSize: 10, color: "#cfd8e2", whiteSpace: "nowrap", textShadow: "0 0 3px #000" }}>{z.name}</span>
          </Html>
        </group>
      ))}
    </>
  );
}

function Switches({ topo, st }: { topo: Topology; st: ViewState }) {
  return (
    <>
      {topo.nodes.filter((n) => n.kind === "switch").map((n) => {
        const sw = st.switches[n.id];
        return (
          <mesh key={n.id} position={[n.x, 2, n.y]}>
            <cylinderGeometry args={[3, 3, 3, 12]} />
            <meshStandardMaterial color={sw?.closed ? "#ff5c5c" : sw?.route_op ? "#5aa9ff" : "#7d8ea0"} />
          </mesh>
        );
      })}
    </>
  );
}

export default function Scene3D({ topo, st }: { topo: Topology; st: ViewState }) {
  const select = useStore((s) => s.select);
  const selection = useStore((s) => s.selection);
  const controls = useRef<any>(null);
  const b = topo.bounds;
  const cx = (b.min_x + b.max_x) / 2, cz = (b.min_y + b.max_y) / 2;
  useEffect(() => { controls.current?.saveState?.(); }, []);
  return (
    <div className="scheme-wrap">
      <div className="scheme-tools">
        <button className="btn small" onClick={() => controls.current?.reset()}>Общий вид</button>
      </div>
      <Canvas camera={{ position: [cx, 820, cz + 330], fov: 42, near: 1, far: 6000 }} dpr={[1, 1.5]}
        onPointerMissed={() => undefined} aria-label="3D-модель станции">
        <color attach="background" args={["#0b1118"]} />
        <ambientLight intensity={0.7} />
        <directionalLight position={[300, 800, 400]} intensity={1.1} />
        <mesh rotation={[-Math.PI / 2, 0, 0]} position={[cx, -0.5, cz]}>
          <planeGeometry args={[b.max_x - b.min_x + 400, b.max_y - b.min_y + 400]} />
          <meshStandardMaterial color="#141c25" />
        </mesh>
        <Tracks topo={topo} st={st} onSelect={(id) => select({ type: "track", id })} selectedId={selection?.type === "track" ? selection.id : undefined} />
        <Switches topo={topo} st={st} />
        <Zones topo={topo} onSelect={(id) => select({ type: "zone", id })} />
        <Trains onSelect={(id) => select({ type: "train", id })} />
        <TrainLabels st={st} />
        <OrbitControls ref={controls} target={[cx, 0, cz]} maxPolarAngle={Math.PI / 2.1} minDistance={80} maxDistance={2500} makeDefault />
      </Canvas>
      <div className="legend">
        <span>Колесо — масштаб, левая кнопка — поворот, правая — перемещение.</span>
        {(["free", "occupied", "unknown", "contradictory", "closed"] as const).map((k) => (
          <span key={k} style={{ color: COLORS[k] }}><b aria-hidden>{TRACK_STATUS[k].icon}</b> {TRACK_STATUS[k].label}</span>
        ))}
      </div>
    </div>
  );
}
