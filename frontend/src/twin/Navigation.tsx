// Навигация по всей карте: полёт с клавиатуры, ограничение камеры границами карты,
// видимые границы карты и станций. Камера двигается только по действиям пользователя.
import { Line } from "@react-three/drei";
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { SCENE } from "./palette";

export interface MapBox { min_x: number; max_x: number; min_z: number; max_z: number }

/** Клавиши полёта: WASD / стрелки — по карте, Q/E (PageDown/PageUp) — ниже/выше, Shift — быстрее. */
const KEYS: Record<string, [number, number, number]> = {
  KeyW: [0, 0, 1], ArrowUp: [0, 0, 1], KeyS: [0, 0, -1], ArrowDown: [0, 0, -1],
  KeyA: [-1, 0, 0], ArrowLeft: [-1, 0, 0], KeyD: [1, 0, 0], ArrowRight: [1, 0, 0],
  KeyQ: [0, -1, 0], PageDown: [0, -1, 0], KeyE: [0, 1, 0], PageUp: [0, 1, 0],
};

/** Активна ли сейчас клавиатурная навигация (не мешаем вводу в поля формы). */
const typing = () => {
  const el = document.activeElement as HTMLElement | null;
  return !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
};

export function KeyFly({ onUserMove }: { onUserMove: () => void }) {
  const { camera, controls, gl } = useThree() as any;
  const down = useRef(new Set<string>());
  useEffect(() => {
    const kd = (e: KeyboardEvent) => {
      if (typing() || !(e.code in KEYS || e.code === "ShiftLeft" || e.code === "ShiftRight")) return;
      // клавиши работают, когда указатель над сценой или фокус на ней
      if (!gl.domElement.matches(":hover") && document.activeElement !== gl.domElement) return;
      if (e.code in KEYS) e.preventDefault();
      down.current.add(e.code);
    };
    const ku = (e: KeyboardEvent) => down.current.delete(e.code);
    const blur = () => down.current.clear();
    window.addEventListener("keydown", kd);
    window.addEventListener("keyup", ku);
    window.addEventListener("blur", blur);
    return () => { window.removeEventListener("keydown", kd); window.removeEventListener("keyup", ku); window.removeEventListener("blur", blur); };
  }, [gl]);
  const fwd = useMemo(() => new THREE.Vector3(), []);
  const right = useMemo(() => new THREE.Vector3(), []);
  const mv = useMemo(() => new THREE.Vector3(), []);
  useFrame((_, dt) => {
    if (!controls || !down.current.size) return;
    let x = 0, y = 0, z = 0;
    for (const k of down.current) { const v = KEYS[k]; if (v) { x += v[0]; y += v[1]; z += v[2]; } }
    if (!x && !y && !z) return;
    const t = controls.target as THREE.Vector3;
    const dist = camera.position.distanceTo(t);
    const fast = down.current.has("ShiftLeft") || down.current.has("ShiftRight") ? 3 : 1;
    const step = dist * 0.9 * fast * Math.min(dt, 0.05);
    // направление «вперёд» — проекция взгляда на землю
    camera.getWorldDirection(fwd); fwd.y = 0;
    if (fwd.lengthSq() < 1e-6) fwd.set(0, 0, -1);
    fwd.normalize();
    right.crossVectors(fwd, camera.up).normalize();
    mv.set(0, 0, 0).addScaledVector(fwd, z * step).addScaledVector(right, x * step);
    camera.position.add(mv); t.add(mv);
    if (y) {
      // высота: приближение/удаление вдоль луча к цели
      const k = Math.exp(y * 1.4 * fast * Math.min(dt, 0.05));
      camera.position.sub(t).multiplyScalar(k).add(t);
    }
    onUserMove();
    controls.update();
  });
  return null;
}

/** Цель камеры не выходит за границы карты, высота — в пределах от minH до maxH. */
export function BoundsClamp({ box, minH, maxH }: { box: MapBox; minH: number; maxH: number }) {
  const { camera, controls } = useThree() as any;
  const d = useMemo(() => new THREE.Vector3(), []);
  useFrame(() => {
    if (!controls) return;
    const t = controls.target as THREE.Vector3;
    const cx = THREE.MathUtils.clamp(t.x, box.min_x, box.max_x);
    const cz = THREE.MathUtils.clamp(t.z, box.min_z, box.max_z);
    if (cx !== t.x || cz !== t.z) {
      d.set(cx - t.x, 0, cz - t.z);
      t.add(d); camera.position.add(d);
    }
    if (camera.position.y < minH) camera.position.y = minH;
    if (camera.position.y > maxH) camera.position.y = maxH;
  }); // компонент стоит последним в сцене: выполняется после OrbitControls и перелётов
  return null;
}

/** Видимая граница: пунктирный прямоугольник с угловыми метками. */
export function MapBorder({ box, y = 1, color = SCENE.accent, dash, width = 1.5, corner }: {
  box: MapBox; y?: number; color?: string; dash: number; width?: number; corner?: number;
}) {
  const pts = useMemo(() => [
    [box.min_x, y, box.min_z], [box.max_x, y, box.min_z], [box.max_x, y, box.max_z], [box.min_x, y, box.max_z], [box.min_x, y, box.min_z],
  ] as [number, number, number][], [box, y]);
  const c = corner ?? Math.min(box.max_x - box.min_x, box.max_z - box.min_z) * 0.06;
  const corners = useMemo(() => {
    const out: [number, number, number][][] = [];
    for (const [x, z, sx, sz] of [[box.min_x, box.min_z, 1, 1], [box.max_x, box.min_z, -1, 1], [box.max_x, box.max_z, -1, -1], [box.min_x, box.max_z, 1, -1]]) {
      out.push([[x + sx * c, y, z], [x, y, z], [x, y, z + sz * c]]);
    }
    return out;
  }, [box, y, c]);
  return (
    <group>
      <Line points={pts} color={color} lineWidth={width} dashed dashSize={dash} gapSize={dash * 0.7} transparent opacity={0.75} />
      {corners.map((p, i) => <Line key={i} points={p} color={color} lineWidth={width * 2.2} />)}
    </group>
  );
}

/** Граница станции на карте сети: прямоугольник по оси станции между горловинами. */
export function StationBorder({ x, z, angle, len, width, color, dash }: { x: number; z: number; angle: number; len: number; width: number; color: string; dash: number }) {
  const pts = useMemo(() => {
    const c = Math.cos(angle), s = Math.sin(angle);
    const loc: [number, number][] = [[-len / 2, -width / 2], [len / 2, -width / 2], [len / 2, width / 2], [-len / 2, width / 2], [-len / 2, -width / 2]];
    return loc.map(([a, b]) => [x + a * c - b * s, 1.2, z + a * s + b * c] as [number, number, number]);
  }, [x, z, angle, len, width]);
  return <Line points={pts} color={color} lineWidth={1.4} dashed dashSize={dash} gapSize={dash * 0.6} transparent opacity={0.85} />;
}
