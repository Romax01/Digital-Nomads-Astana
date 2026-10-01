// Геометрия ломаных: общая для 2D и 3D (координаты схемы от backend).

export function polyLen(p: number[][]) {
  let s = 0;
  for (let i = 0; i < p.length - 1; i++) s += Math.hypot(p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]);
  return s;
}

export function pointAt(p: number[][], s: number): [number, number, number] {
  if (p.length < 2) return [p[0]?.[0] ?? 0, p[0]?.[1] ?? 0, 0];
  if (s <= 0) return [p[0][0], p[0][1], Math.atan2(p[1][1] - p[0][1], p[1][0] - p[0][0])];
  let acc = 0;
  for (let i = 0; i < p.length - 1; i++) {
    const d = Math.hypot(p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]);
    if (acc + d >= s && d > 0) {
      const k = (s - acc) / d;
      return [p[i][0] + (p[i + 1][0] - p[i][0]) * k, p[i][1] + (p[i + 1][1] - p[i][1]) * k, Math.atan2(p[i + 1][1] - p[i][1], p[i + 1][0] - p[i][0])];
    }
    acc += d;
  }
  const a = p[p.length - 2], b = p[p.length - 1];
  return [b[0], b[1], Math.atan2(b[1] - a[1], b[0] - a[0])];
}

/** Участок ломаной между расстояниями s0 и s1. */
export function subPath(p: number[][], s0: number, s1: number): number[][] {
  s0 = Math.max(0, s0); s1 = Math.min(polyLen(p), s1);
  if (s1 <= s0) return [];
  const out: number[][] = [];
  const a = pointAt(p, s0); out.push([a[0], a[1]]);
  let acc = 0;
  for (let i = 0; i < p.length - 1; i++) {
    const d = Math.hypot(p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]);
    acc += d;
    if (acc > s0 && acc < s1) out.push([p[i + 1][0], p[i + 1][1]]);
  }
  const b = pointAt(p, s1); out.push([b[0], b[1]]);
  return out;
}

export const toPath = (p: number[][]) => p.map((q, i) => `${i ? "L" : "M"}${q[0].toFixed(1)},${q[1].toFixed(1)}`).join(" ");

/** Голова состава с интерполяцией между обновлениями backend: только вдоль разрешённого
 *  маршрута и не дальше конечной точки операции (head_end) — анимация не «проезжает» дальше модели. */
export function headNow(pos: { head_s: number; head_end: number; speed: number; moving: boolean }, elapsedModelS: number) {
  if (!pos.moving) return pos.head_s;
  return Math.min(pos.head_end, pos.head_s + pos.speed * Math.max(0, elapsedModelS));
}
