// 3D-цифровой двойник: уровни «Железнодорожная сеть» и «Станция» в одной сцене.
// Визуальный стиль — по мотивам 3darchive (тёмная сцена, сетка, циан-акцент, плавная камера);
// данные, геометрия и проверки — Digital Nomads (backend). Сцена ничего не вычисляет сама:
// занятость, позиции, конфликты и рекомендации приходят с сервера.
import { Grid, OrbitControls } from "@react-three/drei";
import { Canvas, useThree } from "@react-three/fiber";
import * as THREE from "three";
import { useEffect, useMemo, useRef, useState } from "react";
import { fmtHMS } from "../lib/format";
import { TRACK_STATUS } from "../lib/labels";
import { useStore } from "../lib/store";
import type { NetworkStatic, Topology, ViewState } from "../lib/types";
import { CameraRig, fitGoal, type CamGoal } from "./CameraRig";
import { pointAtExt, polyLen } from "./geometry";
import { LabelCtx, LabelLayer, LabelRegistry } from "./labels";
import { enu, NET_M, NetworkLevel, networkBounds, netTrainPoint, useNetTracks } from "./NetworkLevel";
import { SCENE, STATUS_COLOR } from "./palette";
import { StationLevel, STUB_U } from "./StationLevel";
import { trainHeadPoint, TrainsLayer } from "./TrainsLayer";

/** Смена уровня без пересоздания canvas: стартовая позиция камеры и пределы отсечения. */
function LevelSync({ level, far, near, start }: { level: string; far: number; near: number; start: () => CamGoal | null }) {
  const { camera, controls } = useThree() as any;
  const first = useRef(true);
  useEffect(() => {
    camera.far = far; camera.near = near; camera.updateProjectionMatrix();
    if (first.current) { first.current = false; return; }
    const p = start();
    if (p && controls) {
      camera.position.set(...p.position);
      controls.target.set(...p.target);
      controls.update();
    }
  }, [level]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

/** Основной свет (как в 3darchive: направленный 1,8 + заполняющий голубой); цель света — центр уровня. */
function Sun({ position, target, shadows, size }: { position: [number, number, number]; target: [number, number, number]; shadows: boolean; size: number }) {
  const ref = useRef<THREE.DirectionalLight>(null);
  const { scene } = useThree();
  useEffect(() => {
    const l = ref.current;
    if (!l) return;
    l.target.position.set(...target);
    scene.add(l.target);
    const c = l.shadow.camera;
    c.left = -size / 2; c.right = size / 2; c.top = size / 2; c.bottom = -size / 2; c.near = 1; c.far = size * 2;
    c.updateProjectionMatrix();
    return () => { scene.remove(l.target); };
  }, [scene, target[0], target[2], size]); // eslint-disable-line react-hooks/exhaustive-deps
  return <directionalLight ref={ref} position={position} intensity={1.8} castShadow={shadows} shadow-mapSize={[2048, 2048]} shadow-bias={-0.0004} />;
}

function stationGoal(topo: Topology, aspect: number): CamGoal {
  const b = topo.bounds;
  return fitGoal((b.min_x + b.max_x) / 2, (b.min_y + b.max_y) / 2 + 10, (b.max_x - b.min_x) * 0.86, b.max_y - b.min_y + 60, 42, aspect, 0.5);
}

function networkGoal(net: NetworkStatic, aspect: number): CamGoal {
  const b = networkBounds(net);
  return fitGoal((b.min_x + b.max_x) / 2, (b.min_z + b.max_z) / 2, b.max_x - b.min_x + 400, b.max_z - b.min_z + 300, 42, aspect, 0.5);
}

/** Цель камеры для выбранного объекта на текущем уровне (null — объекта нет на этом уровне). */
function selectedGoal(level: string, topo: Topology | null, net: NetworkStatic | null, st: ViewState, netTracks: any, aspect: number): CamGoal | null {
  const sel = useStore.getState().selection;
  if (!sel) return null;
  const near = (x: number, z: number, size: number, y = 0): CamGoal => {
    const g = fitGoal(x, z, size, size * 0.6, 42, aspect);
    g.target[1] = y;
    return g;
  };
  if (level === "network" && net) {
    if (sel.type === "station") {
      const s = net.stations.find((q) => q.id === sel.id);
      if (s) { const [x, z] = enu([s.x_m, s.y_m]); return near(x, z, (s.half_length_m * 2) / NET_M * 2.2); }
    }
    if (sel.type === "section") {
      const s = net.sections.find((q) => q.id === sel.id);
      if (s) {
        const pts = s.centerline.map(enu);
        const xs = pts.map((p) => p[0]), zs = pts.map((p) => p[1]);
        return fitGoal((Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...zs) + Math.max(...zs)) / 2, Math.max(...xs) - Math.min(...xs) + 120, Math.max(...zs) - Math.min(...zs) + 120, 42, aspect);
      }
    }
    if (sel.type === "train") {
      const ph = st.network?.trains[sel.id];
      const p = ph ? netTrainPoint(net, netTracks, ph, 0) : null;
      if (p) return near(p[0], p[2], 160);
    }
    return null;
  }
  if (!topo) return null;
  const track = (id: string) => topo.tracks.find((t) => t.id === id);
  if (sel.type === "track" || sel.type === "operation" || sel.type === "conflict") {
    let tid = sel.id;
    if (sel.type === "operation") tid = st.operations[sel.id]?.track_id ?? "";
    if (sel.type === "conflict") tid = st.conflicts[sel.id]?.objects.find((o) => o.type === "track")?.id ?? "";
    const t = track(tid);
    if (t) { const L = polyLen(t.points); const [x, z] = pointAtExt(t.points, L / 2); return near(x, z, Math.min(900, L + 80)); }
  }
  if (sel.type === "train") { const p = trainHeadPoint(topo, sel.id); if (p) return near(p[0], p[2], 260, p[1]); }
  if (sel.type === "switch") { const n = topo.nodes.find((q) => q.id === sel.id); if (n) return near(n.x, n.y, 120); }
  if (sel.type === "zone") { const z = topo.zones.find((q) => q.id === sel.id); if (z) return near(z.x, z.y, 260); }
  if (sel.type === "device") { const dv = topo.devices.find((q) => q.id === sel.id); if (dv) return near(dv.x, dv.y, 120); }
  if (sel.type === "station") return stationGoal(topo, aspect);
  return null;
}

function Hud({ st, topo, net, netError }: { st: ViewState; topo: Topology | null; net: NetworkStatic | null; netError: string | null }) {
  const level = useStore((s) => s.level);
  const mode = useStore((s) => s.mode);
  const conn = useStore((s) => s.conn);
  const forecastAt = useStore((s) => s.forecastAt);
  const lastMsgAt = useStore((s) => s.lastMsgAt);
  const setCam = useStore((s) => s.setCam);
  const [, tick] = useState(0);
  useEffect(() => { const h = setInterval(() => tick((x) => x + 1), 500); return () => clearInterval(h); }, []);
  const age = lastMsgAt ? (Date.now() - lastMsgAt) / 1000 : null;
  const lost = conn !== "online";
  return (
    <>
      <div className="tw-hud tl">
        <span className="tw-chip level">{level === "network" ? "Уровень: железнодорожная сеть" : `Уровень: станция — ${topo?.station.name ?? st.meta.station_name}`}</span>
        <span className={`tw-chip mode-${mode}`}>{mode === "live" ? "● Сейчас" : mode === "history" ? `⏮ История · ${fmtHMS(st.meta.model_time)}` : `◷ Прогноз${forecastAt ? ` на ${fmtHMS(forecastAt)}` : ""}`}</span>
        {(st.meta.is_demo || net?.source === "demo") && <span className="tw-chip demo" title={net?.note}>Демо-данные{level === "network" ? " · условная сеть" : ""}</span>}
      </div>
      <div className={`tw-hud bl ${lost ? "lost" : ""}`} role={lost ? "alert" : undefined}>
        {lost
          ? <>✕ Связь с сервером потеряна. Показано состояние на {fmtHMS(st.meta.model_time)} (получено {age !== null ? `${Math.round(age)} с назад` : "—"}). Движение составов остановлено.</>
          : mode === "live"
            ? <>Данные сервера: {age !== null ? `${age < 1 ? "<1" : Math.round(age)} с назад` : "—"} · версия {st.meta.state_version} · модельное время {fmtHMS(st.meta.model_time)}{!st.meta.running ? " · симуляция на паузе" : ""}</>
            : mode === "history" ? <>Кадр истории: только просмотр, команды недоступны.</> : <>Прогноз по плану сервера — не наблюдение.</>}
      </div>
      {level === "network" && !net && (
        <div className="tw-center-msg">
          {netError ? <>Модель сети недоступна: {netError}.</> : <>Загрузка модели сети…</>}
          <button className="btn small" onClick={() => setCam("station")}>Перейти к станции</button>
        </div>
      )}
      <div className="tw-hud br legend" aria-label="Обозначения">
        {level === "station" ? (["free", "occupied", "reserved", "unknown", "contradictory", "closed"] as const).map((k) => (
          <span key={k}><i style={{ background: STATUS_COLOR[k] }} /> {TRACK_STATUS[k].icon} {TRACK_STATUS[k].label}</span>
        )) : <>
          <span><i style={{ background: SCENE.accent }} /> Подробная модель</span>
          <span><i style={{ background: "#2d7999" }} /> Упрощённая модель</span>
          <span><i style={{ background: SCENE.warn }} /> Ограничение приёма</span>
        </>}
        <span><i style={{ background: SCENE.bad }} /> ⚠ Конфликт</span>
        <span><i style={{ background: SCENE.select }} /> Выбрано</span>
        <span><i style={{ background: SCENE.highlight }} /> Затронуто рекомендацией</span>
      </div>
    </>
  );
}

export default function TwinScene({ topo, st, net, netError }: { topo: Topology | null; st: ViewState; net: NetworkStatic | null; netError: string | null }) {
  const level = useStore((s) => s.level);
  const cam = useStore((s) => s.cam);
  const camSeq = useStore((s) => s.camSeq);
  const selection = useStore((s) => s.selection);
  const setLevel = useStore((s) => s.setLevel);
  const select = useStore((s) => s.select);
  const overlay = useRef<HTMLDivElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const reg = useMemo(() => new LabelRegistry(), []);
  const netTracks = useNetTracks(net);
  const [goal, setGoal] = useState<{ g: CamGoal | null; seq: number }>({ g: null, seq: 0 });
  const [lostCtx, setLostCtx] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const aspect = () => (wrap.current ? wrap.current.clientWidth / Math.max(1, wrap.current.clientHeight) : 1.6);

  // команды камеры
  useEffect(() => {
    const a = aspect();
    if (cam === "network") { setLevel("network"); if (net) setGoal((p) => ({ g: networkGoal(net, a), seq: p.seq + 1 })); }
    else if (cam === "station") { setLevel("station"); if (topo) setGoal((p) => ({ g: stationGoal(topo, a), seq: p.seq + 1 })); }
    else if (cam === "selected") {
      const g = selectedGoal(level, topo, net, st, netTracks, a);
      if (g) setGoal((p) => ({ g, seq: p.seq + 1 }));
      else {
        // объекта нет на текущем уровне: станция → перелёт на уровень сети, объект станции → уровень станции
        const sel = useStore.getState().selection;
        if (level === "station" && (sel?.type === "section" || (sel?.type === "station" && sel.id !== topo?.station.id))) useStore.getState().setCam("network");
        else if (level === "network" && sel && sel.type !== "station" && sel.type !== "section" && sel.type !== "train") useStore.getState().setCam("station");
      }
    }
  }, [camSeq]); // eslint-disable-line react-hooks/exhaustive-deps

  // смена уровня (поиск, «Вернуться к сети»): общий вид нового уровня; «Выбранный объект» может уточнить его следом
  const firstLevel = useRef(true);
  useEffect(() => {
    if (firstLevel.current) { firstLevel.current = false; return; }
    const a = aspect();
    if (level === "network" && net) setGoal((p) => ({ g: networkGoal(net, a), seq: p.seq + 1 }));
    if (level === "station" && topo) setGoal((p) => ({ g: stationGoal(topo, a), seq: p.seq + 1 }));
  }, [level]); // eslint-disable-line react-hooks/exhaustive-deps

  // первая загрузка данных: вид по уровню
  useEffect(() => { if (level === "station" && topo && !goal.g) setGoal((p) => ({ g: stationGoal(topo, aspect()), seq: p.seq + 1 })); }, [topo]); // eslint-disable-line
  useEffect(() => { if (level === "network" && net && !goal.g) setGoal((p) => ({ g: networkGoal(net, aspect()), seq: p.seq + 1 })); }, [net]); // eslint-disable-line

  // «Следовать за поездом» — только для выбранного состава и только пока включено
  const followId = cam === "follow" && selection?.type === "train" ? selection.id : null;
  useEffect(() => { if (cam === "follow" && selection?.type !== "train") useStore.getState().setCam("free"); }, [cam, selection]);
  const follow = useMemo(() => {
    if (!followId) return null;
    if (level === "station" && topo) return () => trainHeadPoint(topo, followId);
    if (level === "network" && net) return () => {
      const s = useStore.getState();
      const cur = s.mode === "history" ? s.replay.frames[s.replay.index]?.state : s.live;
      const ph = cur?.network?.trains[followId];
      return ph ? netTrainPoint(net, netTracks, ph, 0) : null;
    };
    return null;
  }, [followId, level, topo, net, netTracks]);

  const isNet = level === "network";
  const center = useMemo(() => {
    if (isNet && net) { const b = networkBounds(net); return { x: (b.min_x + b.max_x) / 2, z: (b.min_z + b.max_z) / 2, size: Math.max(b.max_x - b.min_x, b.max_z - b.min_z) + 2000 }; }
    if (topo) { const b = topo.bounds; return { x: (b.min_x + b.max_x) / 2, z: (b.min_y + b.max_y) / 2, size: Math.max(b.max_x - b.min_x, b.max_y - b.min_y) + 2 * STUB_U + 1200 }; }
    return { x: 0, z: 0, size: 2000 };
  }, [isNet, net, topo]);
  // при смене уровня камера ставится над новым уровнем дальше общего вида и «влетает» в него
  const startPose = (): CamGoal | null => {
    const g = isNet ? (net ? networkGoal(net, aspect()) : null) : topo ? stationGoal(topo, aspect()) : null;
    if (!g) return null;
    const k = 1.7;
    return { target: g.target, position: [g.target[0] + (g.position[0] - g.target[0]) * k, g.target[1] + (g.position[1] - g.target[1]) * k, g.target[2] + (g.position[2] - g.target[2]) * k] };
  };
  // начальная позиция камеры = общий вид уровня (без «прыжка» при загрузке)
  const initialPos = (): [number, number, number] => {
    const g = isNet ? (net ? networkGoal(net, aspect()) : null) : topo ? stationGoal(topo, aspect()) : null;
    return g ? g.position : [center.x, center.size * 0.5, center.z + center.size * 0.4];
  };
  const fogNear = isNet ? center.size * 1.3 : center.size * 0.7, fogFar = isNet ? center.size * 3.2 : center.size * 2.2;

  if (lostCtx) return (
    <div className="tw-center-msg static" role="alert">
      Графический контекст WebGL потерян (нехватка видеопамяти или сбой драйвера). Обновите страницу.
      Все данные доступны в разделах «Расписание», «План и Гант», «Датчики и связь».
    </div>
  );

  return (
    <div className="tw-wrap" ref={wrap}>
      <LabelCtx.Provider value={reg}>
        <Canvas shadows dpr={[1, 1.6]} gl={{ antialias: true, powerPreference: "high-performance" }}
          camera={{ position: initialPos(), fov: 42, near: isNet ? 25 : 1, far: center.size * 4 }}
          onPointerMissed={() => undefined} aria-label={isNet ? "3D-модель железнодорожной сети" : "3D-модель станции"}
          onCreated={({ gl }) => gl.domElement.addEventListener("webglcontextlost", (e) => { e.preventDefault(); if (mounted.current) setLostCtx(true); })}>
          <color attach="background" args={[SCENE.bg]} />
          <fog attach="fog" args={[SCENE.bg, fogNear, fogFar]} />
          <ambientLight intensity={0.75} />
          <Sun position={[center.x + center.size * 0.15, center.size * 0.45, center.z + center.size * 0.2]} target={[center.x, 0, center.z]}
            shadows={!isNet} size={center.size} />
          <pointLight position={[center.x - center.size * 0.2, center.size * 0.2, center.z - center.size * 0.1]} intensity={0.9} distance={0} decay={0} color="#2b8db7" />
          <mesh rotation={[-Math.PI / 2, 0, 0]} position={[center.x, -0.6, center.z]} receiveShadow onClick={() => undefined}>
            <planeGeometry args={[center.size * 3, center.size * 3]} />
            <meshStandardMaterial color={SCENE.ground} roughness={0.95} />
          </mesh>
          <Grid position={[center.x, -0.5, center.z]} args={[center.size * 3, center.size * 3]} cellSize={isNet ? 50 : 20} sectionSize={isNet ? 250 : 100}
            cellColor={SCENE.grid} sectionColor={SCENE.gridSection} cellThickness={0.6} sectionThickness={1} fadeDistance={center.size * 1.6} fadeStrength={1.4} infiniteGrid={false} />
          {isNet
            ? (net && <NetworkLevel net={net} topo={topo} st={st} />)
            : (topo && <><StationLevel topo={topo} st={st} net={net} /><TrainsLayer topo={topo} st={st} /></>)}
          <OrbitControls makeDefault enableDamping dampingFactor={0.08} minPolarAngle={0.12} maxPolarAngle={1.42}
            minDistance={isNet ? 20 : 12} maxDistance={center.size * 1.8} target={isNet || !topo ? [center.x, 0, center.z] : stationGoal(topo, aspect()).target} />
          <LevelSync level={level} far={center.size * 4} near={isNet ? 25 : 1} start={startPose} />
          <CameraRig goal={goal.g} goalSeq={goal.seq} follow={follow} />
          <LabelLayer overlay={overlay} reg={reg} />
        </Canvas>
      </LabelCtx.Provider>
      <div className="tw-labels" ref={overlay} aria-hidden="false" />
      <Hud st={st} topo={topo} net={net} netError={netError} />
      {cam === "follow" && followId && !follow?.() && (
        <div className="tw-hud tc">Поезд № {st.trains[followId]?.number ?? ""} не виден на этом уровне{isNet ? " (он на станции)" : " (он на перегоне)"}.
          <button className="btn small" onClick={() => useStore.getState().setCam(isNet ? "station" : "network")}>{isNet ? "К станции" : "К сети"}</button></div>
      )}
      {!selection && <div className="tw-hud tr hint">Колесо — масштаб · левая кнопка — поворот · правая — перемещение · щелчок — карточка</div>}
      {selection && <button className="tw-hud tr btn small" onClick={() => select(null)} title="Снять выбор (только просмотр, состояние не меняется)">Снять выбор ✕</button>}
    </div>
  );
}
