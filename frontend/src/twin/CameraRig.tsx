// Плавная камера (как в 3darchive: сглаживание 1 − e^(−k·dt) отдельно для позиции и цели).
// Камера двигается только по явной команде (кнопки режимов, «Открыть станцию», поиск);
// выбор объекта камеру не трогает. Любое ручное вращение прерывает перелёт.
// «Следовать за поездом» переносит камеру вместе с составом, сохраняя заданный пользователем ракурс.
import { useFrame, useThree } from "@react-three/fiber";
import { useEffect, useRef } from "react";
import * as THREE from "three";
import { useStore } from "../lib/store";
import { damp } from "./geometry";

export interface CamGoal { target: [number, number, number]; position: [number, number, number] }

/** Вид «сверху-сбоку» на прямоугольник: дистанция по размеру и углу обзора. */
export function fitGoal(cx: number, cz: number, w: number, h: number, fov = 42, aspect = 1.6, tilt = 0.62): CamGoal {
  const vf = (fov * Math.PI) / 180;
  const dist = Math.max(h / (2 * Math.tan(vf / 2)), w / (2 * Math.tan(vf / 2) * aspect)) * 1.12 + 40;
  // tilt — угол от вертикали (рад)
  return { target: [cx, 0, cz], position: [cx, dist * Math.cos(tilt), cz + dist * Math.sin(tilt)] };
}

export function CameraRig({ goal, goalSeq, follow }: {
  goal: CamGoal | null; goalSeq: number;
  /** точка поезда для режима «Следовать» (null — поезд не виден на этом уровне) */
  follow: (() => [number, number, number] | null) | null;
}) {
  const { camera, controls } = useThree() as any;
  const active = useRef<CamGoal | null>(null);
  const lastFollow = useRef<THREE.Vector3 | null>(null);
  const tmp = useRef(new THREE.Vector3());
  const latest = useRef<THREE.Vector3 | null>(null);
  useEffect(() => { active.current = goal; }, [goal, goalSeq]);
  useEffect(() => {
    if (!controls) return;
    // ручное вращение прерывает перелёт; слежение (если включено) продолжается сдвигом
    const stop = () => { active.current = null; if (latest.current && !lastFollow.current) lastFollow.current = latest.current; };
    controls.addEventListener("start", stop);
    return () => controls.removeEventListener("start", stop);
  }, [controls]);
  useEffect(() => { lastFollow.current = null; latest.current = null; }, [follow]);
  useFrame((_, dt) => {
    if (!controls) return;
    const t = controls.target as THREE.Vector3;
    let fp: THREE.Vector3 | null = null;
    if (follow) {
      const p = follow();
      if (p) {
        fp = new THREE.Vector3(p[0], p[1], p[2]);
        latest.current = fp;
        if (lastFollow.current) {
          const dx = fp.x - lastFollow.current.x, dz = fp.z - lastFollow.current.z;
          camera.position.x += dx; camera.position.z += dz; t.x += dx; t.z += dz;
          lastFollow.current = fp;
        } else {
          // перелёт к составу с сохранением ракурса; цель обновляется, пока состав движется
          const off = camera.position.clone().sub(t);
          off.setLength(Math.min(off.length(), 420));
          active.current = { target: [fp.x, fp.y, fp.z], position: [fp.x + off.x, fp.y + off.y, fp.z + off.z] };
        }
      }
    }
    const g = active.current;
    if (g) {
      const kp = damp(2.4, Math.min(dt, 0.1)), kt = damp(3.2, Math.min(dt, 0.1));
      camera.position.lerp(tmp.current.set(...g.position), kp);
      t.lerp(tmp.current.set(...g.target), kt);
      if (camera.position.distanceTo(tmp.current.set(...g.position)) < 0.5 && t.distanceTo(tmp.current.set(...g.target)) < 0.5) {
        active.current = null;
        if (fp) lastFollow.current = fp; // дальше — слежение сдвигом
      }
    }
    controls.update();
  });
  return null;
}
