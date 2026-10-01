import { create } from "zustand";
import type { Selection, Topology, ViewState } from "./types";

export type ConnState = "connecting" | "online" | "reconnecting" | "offline";
export type Mode = "live" | "history" | "forecast";

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
  view: "2d" | "3d"; setView: (v: "2d" | "3d") => void;
  toasts: Toast[]; toast: (kind: Toast["kind"], text: string, hint?: string) => void; dismiss: (id: number) => void;
  theme: "dark" | "light"; setTheme: (t: "dark" | "light") => void;
}

const savedTheme = (() => { try { return (localStorage.getItem("ds_theme") as any) || "dark"; } catch { return "dark"; } })();
const savedView = (() => { try { return (localStorage.getItem("ds_view") as any) || "2d"; } catch { return "2d"; } })();
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
  view: savedView, setView: (v) => { try { localStorage.setItem("ds_view", v); } catch { /* */ } set({ view: v }); },
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
