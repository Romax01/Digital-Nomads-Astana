// Процедурные модели подвижного состава и пути (без внешних файлов моделей).
// Кузов: по оси x нормирован к длине 1 (−0,5…0,5), высота и ширина — в метрах.
// Экземпляр масштабируется: x — длина кузова в единицах сцены, y/z — поперечный масштаб.
// Цвета частей заданы цветами вершин; цвет экземпляра только подкрашивает (выбор, неисправность).
import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import { offsetPolyline, type P2 } from "./geometry";

function colored(g: THREE.BufferGeometry, hex: string) {
  const c = new THREE.Color(hex);
  const n = g.attributes.position.count;
  const arr = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) { arr[i * 3] = c.r; arr[i * 3 + 1] = c.g; arr[i * 3 + 2] = c.b; }
  g.setAttribute("color", new THREE.BufferAttribute(arr, 3));
  return g.index ? g.toNonIndexed() : g;
}

/** Параллелепипед: x0..x1 (доля длины), y0..y1 (м), ширина w (м), смещение по z. */
function box(x0: number, x1: number, y0: number, y1: number, w: number, hex: string, z = 0) {
  const g = new THREE.BoxGeometry(x1 - x0, y1 - y0, w);
  g.translate((x0 + x1) / 2, (y0 + y1) / 2, z);
  return colored(g, hex);
}

/** Цилиндр вдоль оси x: длина в долях, радиус в метрах (по вертикали растягивается так же, как ширина). */
function cylX(x0: number, x1: number, yc: number, r: number, hex: string, seg = 14) {
  const g = new THREE.CylinderGeometry(r, r, x1 - x0, seg, 1);
  g.rotateZ(Math.PI / 2);
  g.translate((x0 + x1) / 2, yc, 0);
  return colored(g, hex);
}

function cylY(xc: number, y0: number, y1: number, r: number, hex: string) {
  const g = new THREE.CylinderGeometry(r, r, y1 - y0, 10);
  g.scale(1 / 14, 1, 1); // ось x нормирована к длине ≈14 м
  g.translate(xc, (y0 + y1) / 2, 0);
  return colored(g, hex);
}

const FRAME = "#1d2126";

function merge(parts: THREE.BufferGeometry[]) {
  const g = mergeGeometries(parts.map((p) => { p.deleteAttribute("uv"); return p; }), false)!;
  g.computeVertexNormals();
  g.computeBoundingSphere();
  return g;
}

/** Кузова по видам. Ключ «вид» или «вид_l» — с грузом. */
export function buildBodies(): Record<string, THREE.BufferGeometry> {
  const frame = () => box(-0.5, 0.5, 1.0, 1.28, 2.9, FRAME);
  const ends = (y1: number, hex: string) => [box(-0.5, -0.49, 1.28, y1, 3.1, hex), box(0.49, 0.5, 1.28, y1, 3.1, hex)];
  const gondola = (loaded: boolean) => merge([
    frame(), box(-0.5, 0.5, 1.28, 1.42, 3.0, "#3a2219"),
    box(-0.5, 0.5, 1.42, 3.45, 0.12, "#7b3b27", 1.5), box(-0.5, 0.5, 1.42, 3.45, 0.12, "#7b3b27", -1.5),
    ...ends(3.45, "#6c3322"),
    // рёбра жёсткости
    ...[-0.3, -0.1, 0.1, 0.3].flatMap((x) => [box(x - 0.008, x + 0.008, 1.42, 3.45, 3.14, "#5e2c1d")]),
    ...(loaded ? [box(-0.47, 0.47, 2.9, 3.55, 2.9, "#17181a")] : []),
  ]);
  const covered = () => merge([
    frame(), box(-0.5, 0.5, 1.28, 4.35, 3.15, "#7a4f33"),
    box(-0.5, 0.5, 4.35, 4.62, 2.85, "#8d949b"),
    box(-0.09, 0.09, 1.45, 3.95, 3.2, "#5f3c27"),
    box(-0.003, 0.003, 1.45, 3.95, 3.22, "#2d1d12"),
  ]);
  const tank = (loaded: boolean) => merge([
    frame(), box(-0.46, 0.46, 1.28, 1.5, 2.2, "#2a2d31"),
    cylX(-0.46, 0.46, 2.85, 1.42, loaded ? "#4b5058" : "#3a3f47"),
    cylX(-0.475, -0.46, 2.85, 1.2, "#30343a"), cylX(0.46, 0.475, 2.85, 1.2, "#30343a"),
    cylY(0, 4.1, 4.6, 0.5, "#555c66"),
    box(-0.12, 0.12, 4.25, 4.32, 1.2, "#2b2f35"),
  ]);
  const flat = (loaded: boolean) => merge([
    frame(), box(-0.5, 0.5, 1.28, 1.48, 3.0, "#5b4634"),
    ...[-0.42, -0.14, 0.14, 0.42].flatMap((x) => [box(x - 0.006, x + 0.006, 1.48, 2.1, 0.12, "#2a2522", 1.45), box(x - 0.006, x + 0.006, 1.48, 2.1, 0.12, "#2a2522", -1.45)]),
    ...(loaded ? [box(-0.47, -0.02, 1.48, 4.07, 2.44, "#2f6a99"), box(0.02, 0.47, 1.48, 4.07, 2.44, "#9a3b30"),
      box(-0.47, -0.02, 4.0, 4.07, 2.46, "#25577f"), box(0.02, 0.47, 4.0, 4.07, 2.46, "#7f2f26")] : []),
  ]);
  const hopper = (loaded: boolean) => merge([
    frame(), box(-0.44, 0.44, 1.28, 1.9, 2.0, "#66694d"),
    box(-0.5, 0.5, 1.9, 4.25, 3.15, "#868a5f"),
    box(-0.5, 0.5, 4.25, 4.4, 2.6, loaded ? "#a59b7a" : "#70744f"),
    ...[-0.25, 0, 0.25].map((x) => box(x - 0.01, x + 0.01, 1.9, 4.25, 3.2, "#6e7250")),
  ]);
  const passenger = () => merge([
    frame(), box(-0.5, 0.5, 1.28, 4.1, 3.1, "#2f6b52"),
    box(-0.47, 0.47, 2.55, 3.35, 3.14, "#0f2430"),
    ...Array.from({ length: 12 }, (_, i) => -0.43 + i * 0.078).map((x) => box(x - 0.004, x + 0.004, 2.55, 3.35, 3.16, "#2f6b52")),
    box(-0.5, 0.5, 1.85, 1.98, 3.14, "#d6c35a"),
    box(-0.5, 0.5, 4.1, 4.45, 2.7, "#6f7882"),
    box(-0.5, -0.46, 1.4, 3.9, 3.16, "#24533f"), box(0.46, 0.5, 1.4, 3.9, 3.16, "#24533f"),
  ]);
  // локомотив: кабина в голове (+x), длинный капот, полоса, прожектор
  const loco = () => merge([
    box(-0.5, 0.5, 1.0, 1.55, 3.1, FRAME),
    box(-0.5, 0.26, 1.55, 3.95, 2.55, "#b3302a"),
    box(-0.5, 0.26, 2.05, 2.25, 2.6, "#e6c229"),
    box(0.26, 0.5, 1.55, 4.65, 3.1, "#b3302a"),
    box(0.27, 0.49, 3.45, 4.2, 3.14, "#11202b"),
    box(0.495, 0.502, 3.45, 4.2, 2.4, "#11202b"),
    box(0.26, 0.5, 2.05, 2.25, 3.14, "#e6c229"),
    box(0.497, 0.505, 2.6, 2.95, 0.5, "#fff2b0"),
    box(-0.42, -0.2, 3.95, 4.3, 1.8, "#7d858d"), box(-0.1, 0.1, 3.95, 4.2, 1.4, "#7d858d"),
    box(-0.5, 0.5, 1.55, 1.62, 3.12, "#2b2f35"),
  ]);
  return {
    loco: loco(), passenger: passenger(), covered: covered(), covered_l: covered(),
    gondola: gondola(false), gondola_l: gondola(true), tank: tank(false), tank_l: tank(true),
    flat: flat(false), flat_l: flat(true), hopper: hopper(false), hopper_l: hopper(true),
  };
}

/** Рама тележки (метры по всем осям). */
export function buildBogie() {
  return merge([
    box(-1.35, 1.35, 0.55, 0.95, 2.3, "#22262b"),
    box(-0.25, 0.25, 0.75, 1.02, 2.6, "#2c3137"),
  ].map((g) => g));
}

/** Колёсная пара: ось и два колеса по колее 1520 мм. */
export function buildWheelset() {
  const wheel = (z: number) => { const g = new THREE.CylinderGeometry(0.48, 0.48, 0.14, 16); g.rotateX(Math.PI / 2); g.translate(0, 0.48, z); return colored(g, "#3a3f45"); };
  const axle = new THREE.CylinderGeometry(0.08, 0.08, 1.7, 8); axle.rotateX(Math.PI / 2); axle.translate(0, 0.48, 0);
  return merge([wheel(0.76), wheel(-0.76), colored(axle, "#2a2e33")]);
}

// ------------------------------------------------------------------ путь

/** Лента вдоль ломаной: ширина w, высота h над y0, смещение d от оси (единицы сцены). */
export function ribbon(p: P2[], w: number, h: number, y0 = 0, d = 0): THREE.BufferGeometry {
  const l = offsetPolyline(p, d + w / 2), r = offsetPolyline(p, d - w / 2);
  const pos: number[] = [];
  const quad = (a: number[], b: number[], c: number[], e: number[]) => pos.push(...a, ...b, ...c, ...a, ...c, ...e);
  for (let i = 0; i < p.length - 1; i++) {
    const L0 = [l[i][0], y0 + h, l[i][1]], L1 = [l[i + 1][0], y0 + h, l[i + 1][1]];
    const R0 = [r[i][0], y0 + h, r[i][1]], R1 = [r[i + 1][0], y0 + h, r[i + 1][1]];
    quad(R0, L0, L1, R1); // верх (нормаль вверх)
    if (h > 0) {
      const l0 = [l[i][0], y0, l[i][1]], l1 = [l[i + 1][0], y0, l[i + 1][1]];
      const r0 = [r[i][0], y0, r[i][1]], r1 = [r[i + 1][0], y0, r[i + 1][1]];
      quad(l0, l1, L1, L0); // боковины — нормали наружу
      quad(r0, R0, R1, r1);
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.computeVertexNormals();
  g.computeBoundingSphere();
  return g;
}

/** Две нитки рельсов (колея gauge) по одной оси — одна геометрия на все ломаные. */
export function railsGeometry(polys: P2[][], gauge: number, railW: number, railH: number, y0: number) {
  const parts: THREE.BufferGeometry[] = [];
  for (const p of polys) {
    if (p.length < 2) continue;
    parts.push(ribbon(p, railW, railH, y0, gauge / 2), ribbon(p, railW, railH, y0, -gauge / 2));
  }
  return parts.length ? mergeGeometries(parts, false)! : new THREE.BufferGeometry();
}

/** Балласт (трапеция приближена лентой) по набору ломаных — одна геометрия. */
export function ballastGeometry(polys: P2[][], w: number, h: number) {
  const parts = polys.filter((p) => p.length >= 2).map((p) => ribbon(p, w, h, 0));
  return parts.length ? mergeGeometries(parts, false)! : new THREE.BufferGeometry();
}

/** Освобождение GPU-ресурсов при смене станции/уровня. */
export function disposeAll(objs: (THREE.BufferGeometry | THREE.Material | undefined | null)[]) {
  for (const o of objs) o?.dispose();
}
