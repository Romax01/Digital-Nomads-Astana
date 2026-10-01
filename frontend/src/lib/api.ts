// HTTP-клиент: токен, ключ повтора для изменяющих команд, единый формат ошибок backend.

export class ApiError extends Error {
  code: string; status: number; details: any; hint?: string | null;
  constructor(status: number, code: string, message: string, details?: any, hint?: string | null) {
    super(message); this.status = status; this.code = code; this.details = details; this.hint = hint;
  }
}

let token: string | null = (() => { try { return sessionStorage.getItem("ds_token"); } catch { return null; } })();
export const setToken = (t: string | null) => {
  token = t;
  try { t ? sessionStorage.setItem("ds_token", t) : sessionStorage.removeItem("ds_token"); } catch { /* приватный режим */ }
};
export const getToken = () => token;

export const newKey = () => (crypto as any).randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;

async function request<T>(method: string, url: string, body?: any, opts: { idempotent?: boolean; key?: string } = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.idempotent) headers["Idempotency-Key"] = opts.key ?? newKey();
  let res: Response;
  try {
    res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "NETWORK", "Нет связи с сервером. Проверьте подключение — действие не выполнено.");
  }
  if (res.status === 204) return undefined as T;
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const e = (data && (data as any).error) || {};
    throw new ApiError(res.status, e.code || "HTTP_" + res.status, e.message || "Ошибка запроса", e.details, e.hint);
  }
  return data as T;
}

export const api = {
  get: <T = any>(url: string) => request<T>("GET", url),
  post: <T = any>(url: string, body?: any, key?: string) => request<T>("POST", url, body ?? {}, { idempotent: true, key }),
  put: <T = any>(url: string, body?: any) => request<T>("PUT", url, body ?? {}),
  postPlain: <T = any>(url: string, body?: any) => request<T>("POST", url, body ?? {}),
  del: <T = any>(url: string) => request<T>("DELETE", url, undefined, { idempotent: true }),
};

export async function download(url: string, filename: string) {
  const res = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!res.ok) throw new ApiError(res.status, "DOWNLOAD", "Не удалось выгрузить отчёт");
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
