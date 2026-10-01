// Общие функции мобильного раздела: загрузка фото, защищённые изображения, канал событий.
import { create } from "zustand";
import { ApiError, getToken } from "../lib/api";

export interface MobileCtx {
  user: { id: string; username: string; full_name: string; role: string; role_label: string };
  permissions: string[];
  scope: { station_id: string; station_name: string; pto_id: string | null; brigade_id: string | null; explicit: boolean };
  counts: { unread: number; my_active: number; needs_info: number; awaiting_inspection: number; queue: number };
  dictionaries: { categories: Record<string, string>; urgency: Record<string, string>; work_kinds: Record<string, string>;
    defect_status: Record<string, string>; work_status: Record<string, string> };
  server_time: string;
}

interface MState {
  ctx: MobileCtx | null; setCtx: (c: MobileCtx | null) => void;
  seq: number; bump: () => void;              // изменение данных на сервере (событие WS) → экраны перечитывают
  online: boolean; setOnline: (v: boolean) => void;
  ws: "connecting" | "online" | "offline"; setWs: (v: MState["ws"]) => void;
  sync: { pending: number; sending: boolean; needLogin: boolean; last?: string; error?: string };
  setSync: (p: Partial<MState["sync"]>) => void;
  toast: { text: string; kind: "ok" | "warn" | "error" } | null; say: (text: string, kind?: "ok" | "warn" | "error") => void;
}

export const useM = create<MState>((set) => ({
  ctx: null, setCtx: (c) => set({ ctx: c }),
  seq: 0, bump: () => set((s) => ({ seq: s.seq + 1 })),
  online: typeof navigator === "undefined" ? true : navigator.onLine, setOnline: (v) => set({ online: v }),
  ws: "connecting", setWs: (v) => set({ ws: v }),
  sync: { pending: 0, sending: false, needLogin: false }, setSync: (p) => set((s) => ({ sync: { ...s.sync, ...p } })),
  toast: null,
  say: (text, kind = "ok") => {
    set({ toast: { text, kind } });
    setTimeout(() => set((s) => (s.toast?.text === text ? { toast: null } : {})), kind === "error" ? 8000 : 4000);
  },
}));

export const has = (perm: string) => !!useM.getState().ctx?.permissions.includes(perm);
export const usePerm = (perm: string) => useM((s) => !!s.ctx?.permissions.includes(perm));

/** Загрузка фотографии сырым телом (идемпотентно по client_uuid). */
export async function uploadPhoto(blob: Blob, clientUuid: string, name: string): Promise<string> {
  const token = getToken();
  let res: Response;
  try {
    res = await fetch(`/api/v1/attachments?client_uuid=${encodeURIComponent(clientUuid)}&filename=${encodeURIComponent(name)}`, {
      method: "POST", body: blob, headers: { "Content-Type": blob.type || "image/jpeg", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    });
  } catch {
    throw new ApiError(0, "NETWORK", "Нет связи с сервером");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const e = data?.error || {};
    throw new ApiError(res.status, e.code || "HTTP_" + res.status, e.message || "Не удалось загрузить фото", e.details, e.hint);
  }
  return data.id;
}

/** Защищённые изображения: загрузка с токеном в blob URL (без кэширования service worker). */
const cache = new Map<string, string>();
export async function protectedImage(url: string): Promise<string> {
  if (cache.has(url)) return cache.get(url)!;
  const token = getToken();
  const res = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {}, cache: "no-store" });
  if (!res.ok) throw new ApiError(res.status, "IMAGE", "Фото недоступно");
  const u = URL.createObjectURL(await res.blob());
  cache.set(url, u);
  return u;
}
export function clearImageCache() { for (const u of cache.values()) URL.revokeObjectURL(u); cache.clear(); }

/** Сжатие фото на устройстве: длинная сторона ≤ 1600 px, JPEG ~0,82 (быстрее отправка по мобильной сети). */
export async function compressPhoto(file: Blob, max = 1600): Promise<Blob> {
  try {
    const bmp = await createImageBitmap(file);
    const k = Math.min(1, max / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
    c.getContext("2d")!.drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close?.();
    const out: Blob | null = await new Promise((res) => c.toBlob(res, "image/jpeg", 0.82));
    return out ?? file;
  } catch {
    return file; // формат не декодируется браузером — отправляем как есть (сервер проверит тип)
  }
}

export const uuid = () => (crypto as any).randomUUID ? crypto.randomUUID() : `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}-${Math.random().toString(16).slice(2)}`;

/** Разбор QR-кода: только «DS-WAGON:<номер>» или 8 цифр. Ссылки и прочее не открываются. */
export function parseWagonQr(raw: string): string | null {
  const s = (raw || "").trim();
  const m = /^DS-WAGON:(\d{8})$/i.exec(s) || /^(\d{8})$/.exec(s);
  return m ? m[1] : null;
}

// ------------------------------------------------------------------ ограниченный канал WebSocket
let sock: WebSocket | null = null;
let retry = 0;
let timer: any = null;
export function connectWork() {
  const token = getToken();
  if (!token || sock) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(token)}`);
  sock = ws;
  useM.getState().setWs("connecting");
  ws.onopen = () => { retry = 0; ws.send(JSON.stringify({ type: "hello", last_version: -1 })); };
  ws.onmessage = (ev) => {
    let m: any;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "ping") { ws.send(JSON.stringify({ type: "pong", server_time: m.server_time })); return; }
    if (m.type === "hello_ok") { useM.getState().setWs("online"); useM.getState().bump(); return; }
    if (m.type === "work") useM.getState().bump();
  };
  ws.onclose = () => {
    sock = null;
    useM.getState().setWs("offline");
    if (!getToken()) return;
    clearTimeout(timer);
    timer = setTimeout(connectWork, Math.min(15000, 1000 * 2 ** retry++));
  };
}
export function disconnectWork() { clearTimeout(timer); const s = sock; sock = null; s?.close(); }

export const fmtDT = (iso?: string | null) => iso ? new Date(iso).toLocaleString("ru-RU", { timeZone: "Asia/Almaty", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
export const ago = (iso?: string | null) => {
  if (!iso) return "—";
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  return m < 1 ? "только что" : m < 60 ? `${m} мин назад` : m < 1440 ? `${Math.floor(m / 60)} ч ${m % 60} мин назад` : `${Math.floor(m / 1440)} дн назад`;
};
