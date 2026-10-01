// Применение дельты состояния. Тот же алгоритм, что backend/app/services/view.py::apply_delta,
// используется и для живого режима, и для воспроизведения истории.
import type { ViewState } from "./types";

export const SINGLETONS = ["meta", "index", "plan", "kpi", "maintenance", "network"] as const;
export const COLLECTIONS = ["tracks", "trains", "operations", "resources", "incidents", "conflicts", "recommendations",
  "requests", "alerts", "switches"] as const;

export function applyDelta(state: ViewState, delta: any): ViewState {
  const out: any = { ...state };
  for (const k of Object.keys(delta)) {
    if ((SINGLETONS as readonly string[]).includes(k)) out[k] = delta[k];
    else if ((COLLECTIONS as readonly string[]).includes(k)) {
      const coll = { ...(out[k] || {}) };
      for (const id of delta[k].remove || []) delete coll[id];
      Object.assign(coll, delta[k].upsert || {});
      out[k] = coll;
    }
  }
  return out as ViewState;
}
