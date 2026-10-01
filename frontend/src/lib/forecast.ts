// Режим «Прогноз»: занятость путей на выбранный момент по прогнозному плану (не наблюдение).
import type { ViewState } from "./types";

export function forecastOccupancy(st: ViewState, atIso: string): Record<string, { train_id: string; number: string }> {
  const t = new Date(atIso).getTime();
  const out: Record<string, { train_id: string; number: string }> = {};
  const byTrain: Record<string, any[]> = {};
  for (const o of Object.values(st.operations)) {
    if (!o.train_id || o.status === "cancelled") continue;
    (byTrain[o.train_id] ||= []).push(o);
  }
  for (const [tid, ops] of Object.entries(byTrain)) {
    ops.sort((a, b) => a.seq - b.seq);
    let cur: { track: string; start: number; end: number } | null = null;
    const stays: { track: string; start: number; end: number }[] = [];
    for (const o of ops) {
      const s = new Date(o.forecast_start).getTime(), e = new Date(o.forecast_end).getTime();
      const track = o.kind === "uncoupling" ? o.from_track_id : o.track_id;
      if (o.kind === "arrival" || o.kind === "shunting") {
        if (cur && o.kind === "shunting") { cur.end = e; stays.push(cur); }
        cur = { track, start: s, end: e };
      } else {
        if (!cur) cur = { track, start: s, end: e };
        cur.end = e;
        if (o.kind === "departure") { stays.push(cur); cur = null; }
      }
    }
    if (cur) stays.push(cur);
    const number = ops[0].train_number;
    for (const s of stays) if (s.track && s.start <= t && t < s.end) out[s.track] = { train_id: tid, number };
  }
  return out;
}
