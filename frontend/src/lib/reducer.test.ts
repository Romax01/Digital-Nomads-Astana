import { describe, expect, it } from "vitest";
import { forecastOccupancy } from "./forecast";
import { headNow, pointAt, polyLen, subPath } from "./geom";
import { applyDelta } from "./reducer";

describe("applyDelta — тот же редьюсер для живого режима и истории", () => {
  it("upsert и remove по коллекциям, замена одиночных объектов", () => {
    const s: any = { meta: { v: 1 }, tracks: { a: { s: 1 }, b: { s: 2 } }, trains: {} };
    const d = { meta: { v: 2 }, tracks: { upsert: { a: { s: 9 } }, remove: ["b"] }, trains: { upsert: { t: { n: 1 } }, remove: [] } };
    const r: any = applyDelta(s, d);
    expect(r.meta.v).toBe(2);
    expect(r.tracks).toEqual({ a: { s: 9 } });
    expect(r.trains.t.n).toBe(1);
    expect(s.tracks.b).toBeDefined(); // исходное состояние не изменено (кадры истории неизменяемы)
  });
});

describe("геометрия", () => {
  const p = [[0, 0], [100, 0], [100, 50]];
  it("длина и точка на ломаной", () => {
    expect(polyLen(p)).toBe(150);
    expect(pointAt(p, 120).slice(0, 2)).toEqual([100, 20]);
  });
  it("участок ломаной", () => {
    const s = subPath(p, 50, 120);
    expect(s[0]).toEqual([50, 0]);
    expect(s[s.length - 1]).toEqual([100, 20]);
  });
  it("интерполяция не уходит дальше конечной точки операции", () => {
    expect(headNow({ head_s: 10, head_end: 30, speed: 5, moving: true }, 100)).toBe(30);
    expect(headNow({ head_s: 10, head_end: 30, speed: 5, moving: false }, 100)).toBe(10);
  });
});

describe("прогноз занятости", () => {
  it("путь занят между прибытием и отправлением", () => {
    const op = (id: string, kind: string, seq: number, s: string, e: string) => ({ id, kind, seq, train_id: "T", train_number: "1", track_id: "P1", from_track_id: null, status: "planned", forecast_start: s, forecast_end: e });
    const st: any = { operations: { a: op("a", "arrival", 1, "2026-10-01T07:00:00Z", "2026-10-01T07:10:00Z"), d: op("d", "departure", 2, "2026-10-01T08:00:00Z", "2026-10-01T08:10:00Z") } };
    expect(forecastOccupancy(st, "2026-10-01T07:30:00Z").P1.train_id).toBe("T");
    expect(forecastOccupancy(st, "2026-10-01T09:00:00Z").P1).toBeUndefined();
  });
});
