import { describe, expect, it, vi } from "vitest";
import { observeServer, modelNowMs, resetClock } from "./clock";
import { blendVehicles, smoothHead, type Vehicle } from "./geometry";

describe("плавное движение составов", () => {
  it("голова идёт с предсказанной скоростью и не откатывается назад при поправке сервера", () => {
    let h = 100;
    const rate = 10; // ед/с
    const seq: number[] = [];
    for (let i = 0; i < 60; i++) {
      // сервер присылает чуть отстающие значения (задержка сети): назад не дёргаемся
      const target = 100 + (i + 1) * (1 / 60) * rate - 0.8;
      h = smoothHead(h, target, rate, 1 / 60, 1000);
      seq.push(h);
    }
    for (let i = 1; i < seq.length; i++) expect(seq[i]).toBeGreaterThanOrEqual(seq[i - 1]);
    expect(h).toBeGreaterThan(100);
  });

  it("не выезжает за границу операции и сразу принимает большие расхождения", () => {
    expect(smoothHead(99, 99, 50, 0.5, 100)).toBeLessThanOrEqual(100);
    expect(smoothHead(10, 500, 1, 0.016, 1000)).toBe(500);
  });

  it("перетекание раскладки: в начале — старое положение, в конце — новое", () => {
    const v = (x: number): Vehicle => ({ idx: 0, kind: "loco", lenU: 10, loaded: false, faulty: false, x, y: 0, yaw: 0, fx: x, fy: 0, fyaw: 0, rx: x, ry: 0, ryaw: 0 });
    expect(blendVehicles([v(0)], [v(10)], 0)[0].x).toBeCloseTo(0);
    expect(blendVehicles([v(0)], [v(10)], 1)[0].x).toBeCloseTo(10);
    const mid = blendVehicles([v(0)], [v(10)], 0.5)[0].x;
    expect(mid).toBeGreaterThan(0); expect(mid).toBeLessThan(10);
  });
});

describe("оценка модельного времени", () => {
  it("не зависит от случайной задержки сети и от пингов без модельного времени", () => {
    vi.useFakeTimers();
    resetClock();
    vi.setSystemTime(1_000_000);
    observeServer(1_000.0, "2026-10-02T10:00:00Z", 1, true);      // задержка 0
    const base = modelNowMs()!;
    vi.setSystemTime(1_001_400);                                   // через 1,4 с пришёл пинг с задержкой 400 мс
    observeServer(1_001.0, undefined);
    expect(modelNowMs()! - base).toBeGreaterThan(1380);           // оценка не прыгнула назад на 400 мс (сдвиг < 20 мс)
    vi.setSystemTime(1_002_300);
    observeServer(1_002.0, "2026-10-02T10:00:02Z", 1, true);       // новое модельное время с задержкой 300 мс
    expect(modelNowMs()!).toBeGreaterThanOrEqual(Date.parse("2026-10-02T10:00:02Z"));
    vi.useRealTimers();
  });
});
