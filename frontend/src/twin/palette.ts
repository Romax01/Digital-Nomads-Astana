// Палитра сцены (по мотивам 3darchive: тёмная сцена, циан-акцент) и отображаемые статусы.
// Статус всегда передаётся цветом И текстом/значком.
import { useStore } from "../lib/store";
import { modelNowMs } from "./clock";
import type { TrackState, ViewState } from "../lib/types";

export const SCENE = {
  bg: "#050a10", ground: "#08131c", grid: "#163244", gridSection: "#1f5068",
  accent: "#6de3ff", select: "#68e7ff", highlight: "#ffd166",
  ok: "#31e981", warn: "#ffb340", bad: "#ff4d64",
  ballast: "#4b4f53", rail: "#a9b4bf", sleeper: "#4a3a2c", text: "#d9f5ff",
};

export const STATUS_COLOR: Record<string, string> = {
  free: "#2fbf7a", occupied: "#4f9dff", reserved: "#ffb340", unknown: "#b892ff",
  contradictory: "#ff9f43", closed: "#ff4d64",
};

export const KIND_COLOR: Record<string, string> = { freight: "#c9d4df", transfer: "#7fd0ff", passenger: "#f5cf5a" };

/** Отображаемый статус пути. «Зарезервирован» — свободный путь с серверным резервом,
 *  начинающимся в ближайшие 30 модельных минут (данные backend, клиент ничего не вычисляет сам). */
export function trackDisplay(ts: TrackState | undefined, modelNowMs: number): string {
  if (!ts) return "unknown";
  if (ts.status === "free" && ts.next_reservation) {
    const s = new Date(ts.next_reservation.start).getTime();
    if (s - modelNowMs < 30 * 60_000) return "reserved";
  }
  return ts.status;
}

/** Состояние для кадра анимации и прошедшее модельное время с последнего сообщения сервера.
 *  При потере связи и вне режима «Сейчас» интерполяция не выполняется (elapsed = 0). */
export function frameState(): { st: ViewState | null; elapsed: number } {
  const s = useStore.getState();
  const st = s.mode === "history" ? s.replay.frames[s.replay.index]?.state ?? null : s.live;
  if (!st || s.mode !== "live" || s.conn !== "online" || !st.meta.running) return { st, elapsed: 0 };
  // прошло модельного времени с момента, на который сервер рассчитал позиции (по часам сервера)
  const now = modelNowMs();
  if (now === null) return { st, elapsed: 0 };
  const el = (now - Date.parse(st.meta.model_time)) / 1000;
  return { st, elapsed: Math.min(5 * Math.max(1, st.meta.speed), Math.max(0, el)) };
}
