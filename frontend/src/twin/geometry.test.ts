import { describe, expect, it } from "vitest";
import { fracNow, headNow, layoutConsist, layoutLabels, offsetPolyline, pointAtExt, polyLen, sampleAlong } from "./geometry";

// дуга радиуса 100 на 90° + прямая: типичный отклоняющийся маршрут
const arc: number[][] = [];
for (let i = 0; i <= 90; i++) { const a = (i * Math.PI) / 180; arc.push([100 * Math.sin(a), 100 - 100 * Math.cos(a)]); }
const path = [[-400, 0], ...arc.slice(1).map((p) => [p[0], p[1]]), [100, 400]];

const distToPath = (x: number, y: number) => {
  let best = Infinity;
  for (let i = 0; i < path.length - 1; i++) {
    const [ax, ay] = path[i], [bx, by] = path[i + 1];
    const dx = bx - ax, dy = by - ay;
    const t = Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)));
    best = Math.min(best, Math.hypot(x - ax - t * dx, y - ay - t * dy));
  }
  return best;
};

describe("единая геометрия", () => {
  it("тележки каждого вагона лежат на той же ломаной, что и рельсы", () => {
    const consist = { loco_length_m: 34, source: "wagons", groups: [{ kind: "gondola", length_m: 13.92, count: 30, loaded: true, faulty: false }] };
    const veh = layoutConsist(path, 600, 451.6 * 0.68, consist, 30, "freight");
    expect(veh).toHaveLength(31); // локомотив + все вагоны — количество не сокращается
    for (const v of veh) {
      expect(distToPath(v.fx, v.fy)).toBeLessThan(0.05);
      expect(distToPath(v.rx, v.ry)).toBeLessThan(0.05);
    }
  });

  it("длина состава равна длине от сервера, вагоны не налезают друг на друга", () => {
    const consist = { loco_length_m: 34, source: "wagons", groups: [{ kind: "tank", length_m: 12.02, count: 10, loaded: false, faulty: false }] };
    const body = (34 + 120.2) * 0.7;
    const veh = layoutConsist(path, 560, body, consist, 10, "freight"); // состав на кривой
    const total = veh.reduce((a, v) => a + v.lenU, 0);
    expect(total).toBeLessThan(body);
    expect(total).toBeGreaterThan(body * 0.9); // минус зазоры сцепок
    // на кривой вагон повёрнут по касательной между своими тележками, а не по направлению всего состава
    const yaws = new Set(veh.map((v) => Math.round(v.yaw * 100)));
    expect(yaws.size).toBeGreaterThan(3);
  });

  it("хвост ожидающего у входа состава продолжается по касательной, а не складывается в точку", () => {
    const p = pointAtExt([[0, 0], [10, 0]], -25);
    expect(p[0]).toBeCloseTo(-25);
    expect(p[1]).toBeCloseTo(0);
  });

  it("интерполяция не выходит за границу операции", () => {
    expect(headNow({ head_s: 10, head_end: 50, speed: 2, moving: true }, 100)).toBe(50);
    expect(headNow({ head_s: 10, head_end: 50, speed: 2, moving: false }, 100)).toBe(10);
    expect(fracNow({ frac: 0.9, frac_rate: 0.01, frac_max: 1 }, 100)).toBe(1);
  });

  it("смещённая линия параллельна исходной", () => {
    const o = offsetPolyline([[0, 0], [100, 0], [200, 0]], 5);
    expect(o.every((p) => Math.abs(p[1] - 5) < 1e-9)).toBe(true);
    expect(polyLen(o)).toBeCloseTo(200);
    expect(sampleAlong([[0, 0], [100, 0]], 10)).toHaveLength(10);
  });
});

describe("подписи", () => {
  it("не накладываются и выбираются по приоритету с ограничением числа", () => {
    const c = [
      { id: "a", x: 100, y: 100, w: 80, h: 20, priority: 10 },
      { id: "b", x: 110, y: 105, w: 80, h: 20, priority: 90 },
      { id: "c", x: 400, y: 100, w: 80, h: 20, priority: 5 },
      { id: "d", x: 700, y: 100, w: 80, h: 20, priority: 1 },
      { id: "off", x: -500, y: 100, w: 80, h: 20, priority: 99 },
    ];
    const v = layoutLabels(c, 2, 1000, 600);
    expect([...v]).toEqual(["b", "c"]);
  });
});

describe("геометрия лент пути", () => {
  it("верхняя грань ленты смотрит вверх (иначе она отсекается и путь «пропадает»)", async () => {
    const { ribbon } = await import("./models");
    const g = ribbon([[0, 0], [10, 0], [20, 5]], 2, 0, 0);
    g.computeVertexNormals();
    const n = g.attributes.normal.array;
    for (let i = 1; i < n.length; i += 3) expect(n[i]).toBeGreaterThan(0.9);
  });
});
