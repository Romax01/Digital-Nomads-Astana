// Единая геометрия 3D-двойника. Рельсы, подсветка маршрута, локомотив и каждый вагон
// строятся по одной и той же ломаной от backend (координаты схемы станции или метры ENU сети).
// Здесь нет случайных величин: одинаковые данные дают одинаковую сцену.

export type P2 = number[]; // [x, y] — координаты схемы (y схемы → z сцены)

export function polyLen(p: P2[]) {
  let s = 0;
  for (let i = 0; i < p.length - 1; i++) s += Math.hypot(p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]);
  return s;
}

/** Накопленные длины вершин — для многократных запросов точки по одной ломаной. */
export function cumulative(p: P2[]): number[] {
  const c = [0];
  for (let i = 0; i < p.length - 1; i++) c.push(c[i] + Math.hypot(p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]));
  return c;
}

/** Точка и направление на расстоянии s. За пределами ломаной — продолжение по касательной
 *  крайнего отрезка (хвост ожидающего у входа поезда стоит на подходе, а не «складывается»). */
export function pointAtExt(p: P2[], s: number, cum?: number[]): [number, number, number] {
  const n = p.length;
  if (n < 2) return [p[0]?.[0] ?? 0, p[0]?.[1] ?? 0, 0];
  const c = cum ?? cumulative(p);
  const total = c[n - 1];
  if (s <= 0) {
    const a = p[0], b = p[1];
    const d = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    return [a[0] + (b[0] - a[0]) / d * s, a[1] + (b[1] - a[1]) / d * s, Math.atan2(b[1] - a[1], b[0] - a[0])];
  }
  if (s >= total) {
    const a = p[n - 2], b = p[n - 1];
    const d = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const e = s - total;
    return [b[0] + (b[0] - a[0]) / d * e, b[1] + (b[1] - a[1]) / d * e, Math.atan2(b[1] - a[1], b[0] - a[0])];
  }
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (c[m] <= s) lo = m; else hi = m; }
  const a = p[lo], b = p[lo + 1];
  const d = c[lo + 1] - c[lo] || 1;
  const k = (s - c[lo]) / d;
  return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, Math.atan2(b[1] - a[1], b[0] - a[0])];
}

/** Параллельная линия на расстоянии d (слева по ходу при d > 0, ось y схемы — вниз).
 *  Та же формула, что backend/app/domain/network.py::offset_polyline. */
export function offsetPolyline(p: P2[], d: number): P2[] {
  const n = p.length;
  const out: P2[] = [];
  for (let i = 0; i < n; i++) {
    const a = p[Math.max(0, i - 1)], b = p[Math.min(n - 1, i + 1)];
    const tx = b[0] - a[0], ty = b[1] - a[1];
    const ln = Math.hypot(tx, ty) || 1;
    out.push([p[i][0] - ty / ln * d, p[i][1] + tx / ln * d]);
  }
  return out;
}

/** Точки с равным шагом вдоль ломаной (шпалы, столбы): [x, y, угол]. */
export function sampleAlong(p: P2[], step: number, offset = step / 2): [number, number, number][] {
  const c = cumulative(p);
  const total = c[c.length - 1];
  const out: [number, number, number][] = [];
  for (let s = offset; s < total; s += step) out.push(pointAtExt(p, s, c));
  return out;
}

// ------------------------------------------------------------------ составы

export interface ConsistGroup { kind: string; length_m: number | null; count: number; loaded: boolean; faulty: boolean }
export interface Consist { loco_length_m: number; source: string; groups: ConsistGroup[] }

export const DEFAULT_LEN_M: Record<string, number> = {
  loco: 34, passenger: 24.5, gondola: 13.92, covered: 17.64, tank: 12.02, flat: 14.62, hopper: 14.72, container: 14.62,
};

export interface Vehicle {
  idx: number;          // порядковый номер от головы (0 — локомотив)
  kind: string;         // loco | passenger | gondola | tank | covered | flat | hopper
  lenU: number;         // длина кузова в единицах сцены (с учётом зазора сцепки)
  loaded: boolean; faulty: boolean;
  x: number; y: number; yaw: number;          // центр между тележками и направление
  fx: number; fy: number; fyaw: number; rx: number; ry: number; ryaw: number; // тележки: точки и касательные
}

/** Раскладка локомотива и каждого вагона вдоль пути.
 *  path — ломаная движения (в направлении движения), head — расстояние головы от начала,
 *  bodyU — длина изображения состава от backend (фактическая длина × масштаб станции).
 *  Длины единиц пропорциональны фактическим, число вагонов — из данных (не сокращается).
 *  Каждый вагон ставится по двум точкам тележек на той же ломаной, поэтому на кривых и в
 *  стрелочных переводах состав изгибается, а не «режет» угол. */
export function layoutConsist(path: P2[], head: number, bodyU: number, consist: Consist | undefined,
  wagonsCount: number, trainKind: string, gapM = 0.9): Vehicle[] {
  const units: { kind: string; len: number; loaded: boolean; faulty: boolean }[] = [];
  units.push({ kind: "loco", len: consist?.loco_length_m || DEFAULT_LEN_M.loco, loaded: false, faulty: false });
  if (consist && consist.groups.length) {
    for (const g of consist.groups) {
      for (let i = 0; i < g.count; i++) units.push({ kind: g.kind, len: g.length_m || DEFAULT_LEN_M[g.kind] || 14, loaded: g.loaded, faulty: g.faulty });
    }
  } else {
    const kind = trainKind === "passenger" ? "passenger" : "covered";
    for (let i = 0; i < wagonsCount; i++) units.push({ kind, len: DEFAULT_LEN_M[kind], loaded: false, faulty: false });
  }
  const totalM = units.reduce((a, u) => a + u.len, 0);
  const k = totalM > 0 ? bodyU / totalM : 1; // единиц сцены на метр (= масштаб станции)
  const cum = cumulative(path);
  const out: Vehicle[] = [];
  let s = head;
  units.forEach((u, idx) => {
    const lenU = u.len * k;
    const body = Math.max(lenU - gapM * k, lenU * 0.85);
    const bogie = body * 0.36; // расстояние центра тележки от центра кузова
    const sc = s - lenU / 2;
    const f = pointAtExt(path, sc + bogie, cum);
    const r = pointAtExt(path, sc - bogie, cum);
    out.push({
      idx, kind: u.kind, lenU: body, loaded: u.loaded, faulty: u.faulty,
      x: (f[0] + r[0]) / 2, y: (f[1] + r[1]) / 2, yaw: Math.atan2(f[1] - r[1], f[0] - r[0]),
      fx: f[0], fy: f[1], fyaw: f[2], rx: r[0], ry: r[1], ryaw: r[2],
    });
    s -= lenU;
  });
  return out;
}

/** Голова состава с интерполяцией между обновлениями сервера. Не дальше границы операции
 *  (head_end); при потере связи интерполяция останавливается (elapsed = 0 у вызывающего). */
export function headNow(pos: { head_s: number; head_end: number; speed: number; moving: boolean }, elapsedModelS: number) {
  if (!pos.moving) return pos.head_s;
  const h = pos.head_s + pos.speed * Math.max(0, elapsedModelS);
  return pos.speed >= 0 ? Math.min(pos.head_end, h) : Math.max(pos.head_end, h);
}

/** Доля пройденного перегона с интерполяцией, не дальше frac_max. */
export function fracNow(ph: { frac: number; frac_rate: number; frac_max: number }, elapsedModelS: number) {
  return Math.min(ph.frac_max, ph.frac + ph.frac_rate * Math.max(0, elapsedModelS));
}

// ------------------------------------------------------------------ подписи

export interface LabelCand { id: string; x: number; y: number; w: number; h: number; priority: number }

/** Выбор подписей без наложения: по убыванию приоритета, жадно, не больше maxN и в пределах экрана. */
export function layoutLabels(cands: LabelCand[], maxN: number, vw: number, vh: number, pad = 1): Set<string> {
  const placed: { x0: number; y0: number; x1: number; y1: number }[] = [];
  const out = new Set<string>();
  const sorted = [...cands].sort((a, b) => b.priority - a.priority || a.id.localeCompare(b.id));
  for (const c of sorted) {
    if (out.size >= maxN) break;
    const r = { x0: c.x - c.w / 2 - pad, y0: c.y - c.h - pad, x1: c.x + c.w / 2 + pad, y1: c.y + pad };
    if (r.x1 < 0 || r.y1 < 0 || r.x0 > vw || r.y0 > vh) continue;
    if (placed.some((q) => r.x0 < q.x1 && r.x1 > q.x0 && r.y0 < q.y1 && r.y1 > q.y0)) continue;
    placed.push(r);
    out.add(c.id);
  }
  return out;
}

/** Плавное приближение (как в 3darchive: 1 − e^(−k·dt)), не зависит от частоты кадров. */
export const damp = (k: number, dt: number) => 1 - Math.exp(-k * dt);
