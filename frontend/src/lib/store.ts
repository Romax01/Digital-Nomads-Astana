import { create } from "zustand";
import type { NetworkStatic, Selection, Topology, ViewState } from "./types";

export type ConnState = "connecting" | "online" | "reconnecting" | "offline";
export type Mode = "live" | "history" | "forecast";

export type Level = "network" | "station";
/** Режимы камеры. «Следовать за поездом» включается явно и выключается той же кнопкой. */
export type CamMode = "network" | "station" | "selected" | "follow" | "free";

export interface Toast { id: number; kind: "ok" | "error" | "info" | "warn"; text: string; hint?: string }

export interface ReplayFrame { real_time: string; model_time: string; state: ViewState }

interface Store {
  user: any | null; setUser: (u: any | null) => void;
  live: ViewState | null; version: number; lastMsgAt: number; serverOffsetMs: number; rttMs: number | null;
  conn: ConnState; setConn: (c: ConnState) => void;
  topology: Topology | null; setTopology: (t: Topology | null) => void;
  mode: Mode; setMode: (m: Mode) => void;
  forecastAt: string | null; setForecastAt: (t: string | null) => void;
  replay: { frames: ReplayFrame[]; index: number; playing: boolean; speed: number; loading: boolean; error?: string };
  setReplay: (p: Partial<Store["replay"]>) => void;
  selection: Selection; select: (s: Selection) => void;
  network: NetworkStatic | null; networkError: string | null; setNetwork: (n: NetworkStatic | null, err?: string | null) => void;
  /** 3D-двойник: уровень, режим камеры, подсветка рекомендации, панели. Выбор — только просмотр. */
  level: Level; setLevel: (l: Level) => void;
  cam: CamMode; camSeq: number; setCam: (c: CamMode) => void;
  highlight: { ids: string[]; source: string } | null; setHighlight: (h: Store["highlight"]) => void;
  panels: { right: boolean; bottom: boolean; ganttFull: boolean; fullscreen: boolean };
  setPanels: (p: Partial<Store["panels"]>) => void;
  workSeq: number; // событие процесса работников (WS) — экраны сообщений перечитывают данные
  toasts: Toast[]; toast: (kind: Toast["kind"], text: string, hint?: string) => void; dismiss: (id: number) => void;
  theme: "dark" | "light"; setTheme: (t: "dark" | "light") => void;
}

const savedTheme = (() => { try { return (localStorage.getItem("ds_theme") as any) || "dark"; } catch { return "dark"; } })();
const savedPanels = (() => { try { return JSON.parse(localStorage.getItem("ds_panels") || "null"); } catch { return null; } })();
let toastId = 1;

export const useStore = create<Store>((set) => ({
  user: null, setUser: (u) => set({ user: u }),
  live: null, version: 0, lastMsgAt: 0, serverOffsetMs: 0, rttMs: null,
  conn: "connecting", setConn: (c) => set({ conn: c }),
  topology: null, setTopology: (t) => set({ topology: t }),
  mode: "live", setMode: (m) => set({ mode: m }),
  forecastAt: null, setForecastAt: (t) => set({ forecastAt: t }),
  replay: { frames: [], index: 0, playing: false, speed: 1, loading: false },
  setReplay: (p) => set((s) => ({ replay: { ...s.replay, ...p } })),
  selection: null, select: (s) => set({ selection: s }),
  network: null, networkError: null, setNetwork: (n, err = null) => set({ network: n, networkError: err }),
  level: "station", setLevel: (l) => set({ level: l }),
  cam: "station", camSeq: 0, setCam: (c) => set((s) => ({ cam: c, camSeq: s.camSeq + 1 })),
  highlight: null, setHighlight: (h) => set({ highlight: h }),
  panels: { right: true, bottom: true, ganttFull: false, ...(savedPanels || {}), fullscreen: false },
  setPanels: (p) => set((s) => {
    const panels = { ...s.panels, ...p };
    try { localStorage.setItem("ds_panels", JSON.stringify({ right: panels.right, bottom: panels.bottom, ganttFull: panels.ganttFull })); } catch { /* */ }
    return { panels };
  }),
  workSeq: 0,
  toasts: [],
  toast: (kind, text, hint) => {
    const id = toastId++;
    set((s) => ({ toasts: [...s.toasts.slice(-4), { id, kind, text, hint }] }));
    setTimeout(() => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), kind === "error" ? 12000 : 6000);
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
  theme: savedTheme, setTheme: (t) => { try { localStorage.setItem("ds_theme", t); } catch { /* */ } set({ theme: t }); },
}));

/** Состояние, которое видит пользователь: живое или кадр истории. */
export function useViewState(): ViewState | null {
  const mode = useStore((s) => s.mode);
  const live = useStore((s) => s.live);
  const frame = useStore((s) => s.replay.frames[s.replay.index]);
  if (mode === "history") return frame?.state ?? null;
  return live;
}

export const can = (user: any, action: string) => !!user?.permissions?.includes(action);
