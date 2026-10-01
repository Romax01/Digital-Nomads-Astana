// Офлайн-очередь и черновики в IndexedDB. Разделены по пользователю: после смены учётной записи
// данные прошлого пользователя не отправляются. Статусы — только фактические:
// «Черновик» (drafts), «Ожидает отправки» (pending), «Отправляется» (sending), «Ошибка» (error);
// «Доставлено» — после ответа сервера (запись удаляется из очереди, сообщение видно в «Мои сообщения»).
import { api, ApiError } from "../lib/api";
import { uploadPhoto, useM } from "./mlib";

export interface OutPhoto { id: string; name: string; type: string; blob: Blob }
export interface OutItem {
  id: string; userId: string; status: "pending" | "sending" | "error"; error?: string; createdAt: string;
  payload: Record<string, any>; photos: OutPhoto[]; uploaded: Record<string, string>; attempts: number;
}

const DB = "ds-mobile";
let dbp: Promise<IDBDatabase> | null = null;
function db(): Promise<IDBDatabase> {
  if (!dbp) {
    dbp = new Promise((res, rej) => {
      const r = indexedDB.open(DB, 1);
      r.onupgradeneeded = () => {
        const d = r.result;
        if (!d.objectStoreNames.contains("outbox")) d.createObjectStore("outbox", { keyPath: "id" }).createIndex("user", "userId");
        if (!d.objectStoreNames.contains("drafts")) d.createObjectStore("drafts", { keyPath: "userId" });
      };
      r.onsuccess = () => res(r.result);
      r.onerror = () => { dbp = null; rej(r.error); };
    });
  }
  return dbp;
}

async function tx<T>(store: string, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T> | void): Promise<T | undefined> {
  const d = await db();
  return new Promise((res, rej) => {
    const t = d.transaction(store, mode);
    const s = t.objectStore(store);
    const r = fn(s);
    t.oncomplete = () => res(r ? (r as IDBRequest<T>).result : undefined);
    t.onerror = () => rej(t.error);
    t.onabort = () => rej(t.error);
  });
}

export const putItem = (it: OutItem) => tx("outbox", "readwrite", (s) => { s.put(it); });
export const delItem = (id: string) => tx("outbox", "readwrite", (s) => { s.delete(id); });
export async function listItems(userId?: string): Promise<OutItem[]> {
  const all = (await tx<OutItem[]>("outbox", "readonly", (s) => s.getAll())) ?? [];
  return all.filter((x) => !userId || x.userId === userId).sort((a, b) => a.createdAt.localeCompare(b.createdAt));
}
export async function otherUsersPending(userId: string): Promise<number> {
  return (await listItems()).filter((x) => x.userId !== userId).length;
}

export const saveDraft = (userId: string, draft: any) => tx("drafts", "readwrite", (s) => { s.put({ userId, draft, savedAt: new Date().toISOString() }); });
export const loadDraft = async (userId: string) => ((await tx<any>("drafts", "readonly", (s) => s.get(userId))) ?? null);
export const clearDraft = (userId: string) => tx("drafts", "readwrite", (s) => { s.delete(userId); });

/** Сообщение ставится в очередь всегда (и онлайн): единый путь, повтор по client_uuid безопасен. */
export async function enqueue(userId: string, payload: Record<string, any>, photos: OutPhoto[]) {
  const it: OutItem = { id: payload.client_uuid, userId, status: "pending", createdAt: new Date().toISOString(),
    payload, photos, uploaded: {}, attempts: 0 };
  await putItem(it);
  await refreshCount(userId);
  return it;
}

export async function refreshCount(userId?: string) {
  const uid = userId ?? useM.getState().ctx?.user.id;
  if (!uid) return;
  const items = await listItems(uid);
  useM.getState().setSync({ pending: items.length });
}

let running = false;
/** Отправка очереди текущего пользователя: фото → сообщение. Один раз для каждого элемента,
 *  результат сверяется с сервером (idempotent_replay). Истёкшая сессия — данные остаются. */
export async function syncNow(userId: string): Promise<{ sent: number; failed: number }> {
  if (running) return { sent: 0, failed: 0 };
  running = true;
  const st = useM.getState();
  st.setSync({ sending: true, error: undefined });
  let sent = 0, failed = 0;
  try {
    for (const it of await listItems(userId)) {
      if (it.status === "error" && it.attempts >= 1 && it.error?.startsWith("!")) { failed++; continue; } // требует действий пользователя
      it.status = "sending"; it.attempts += 1;
      await putItem(it);
      try {
        for (const p of it.photos) {
          if (!it.uploaded[p.id]) {
            it.uploaded[p.id] = await uploadPhoto(p.blob, p.id, p.name);
            await putItem(it);
          }
        }
        const res = await api.postPlain("/api/v1/defect-reports", { ...it.payload, attachment_ids: it.photos.map((p) => it.uploaded[p.id]) });
        await delItem(it.id);
        sent++;
        useM.getState().say(res.idempotent_replay ? `Сообщение № ${res.number} уже было доставлено` : `Доставлено: сообщение № ${res.number}`);
      } catch (e) {
        const err = e as ApiError;
        if (err.status === 401) {
          it.status = "pending"; it.error = "Сессия истекла — войдите снова, сообщение сохранено";
          await putItem(it);
          useM.getState().setSync({ needLogin: true });
          break;
        }
        if (err.status === 0 || err.status >= 500) {
          it.status = "pending"; it.error = "Нет связи с сервером — повторим при восстановлении связи";
          await putItem(it);
          break;
        }
        it.status = "error"; it.error = "!" + (err.message || "Ошибка отправки");
        await putItem(it);
        failed++;
      }
    }
  } finally {
    running = false;
    st.setSync({ sending: false, last: new Date().toISOString() });
    await refreshCount(userId);
  }
  if (sent) useM.getState().bump();
  return { sent, failed };
}

export async function retryItem(id: string) {
  const it = (await listItems()).find((x) => x.id === id);
  if (!it) return;
  it.status = "pending"; it.error = undefined; it.attempts = 0;
  await putItem(it);
}
