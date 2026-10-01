// Форма «Сообщить о дефекте». Черновик сохраняется на устройстве (IndexedDB) автоматически;
// отправка всегда идёт через очередь: без связи сообщение ждёт и уходит один раз после восстановления.
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { compressPhoto, parseWagonQr, useM, uuid } from "./mlib";
import { clearDraft, enqueue, loadDraft, saveDraft, syncNow } from "./outbox";

const KIND_RU: Record<string, string> = { gondola: "полувагон", covered: "крытый", tank: "цистерна", flat: "платформа", hopper: "хоппер", passenger: "пассажирский" };

interface Draft {
  wagon: any | null; manual: string; track_id: string; position: string; category: string; component: string;
  description: string; urgency: string; photos: { id: string; blob: Blob; name: string }[]; geo: boolean;
}
const EMPTY: Draft = { wagon: null, manual: "", track_id: "", position: "", category: "", component: "", description: "", urgency: "normal", photos: [], geo: false };

function QrScanner({ onCode, onClose }: { onCode: (n: string) => void; onClose: () => void }) {
  const video = useRef<HTMLVideoElement>(null);
  const [msg, setMsg] = useState("Наведите камеру на QR-код вагона");
  useEffect(() => {
    let stream: MediaStream | null = null, stop = false;
    const BD = (window as any).BarcodeDetector;
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
        if (!video.current) return;
        video.current.srcObject = stream;
        await video.current.play();
        const det = new BD({ formats: ["qr_code"] });
        while (!stop) {
          const codes = await det.detect(video.current).catch(() => []);
          if (codes.length) {
            const n = parseWagonQr(codes[0].rawValue);
            if (n) { onCode(n); return; }
            setMsg("Код не распознан как номер вагона. Ссылки из QR-кодов не открываются — используйте поиск.");
          }
          await new Promise((r) => setTimeout(r, 300));
        }
      } catch {
        setMsg("Камера недоступна. Разрешите доступ к камере или найдите вагон по номеру.");
      }
    })();
    return () => { stop = true; stream?.getTracks().forEach((t) => t.stop()); };
  }, []);
  return (
    <div className="m-sheet" role="dialog" aria-label="Сканирование QR-кода">
      <video ref={video} className="m-qr-video" playsInline muted />
      <p>{msg}</p>
      <button className="m-btn" onClick={onClose}>Закрыть</button>
    </div>
  );
}

export default function ReportForm({ onSent }: { onSent: () => void }) {
  const ctx = useM((s) => s.ctx)!;
  const online = useM((s) => s.online);
  const uid = ctx.user.id;
  const [d, setD] = useState<Draft>(EMPTY);
  const [loaded, setLoaded] = useState(false);
  const [q, setQ] = useState("");
  const [found, setFound] = useState<any[] | null>(null);
  const [trains, setTrains] = useState<any[] | null>(null);
  const [tracks, setTracks] = useState<any[]>([]);
  const [qr, setQr] = useState(false);
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const cam = useRef<HTMLInputElement>(null);
  const gal = useRef<HTMLInputElement>(null);
  const urls = useRef(new Map<string, string>());
  const purl = (p: { id: string; blob: Blob }) => { if (!urls.current.has(p.id)) urls.current.set(p.id, URL.createObjectURL(p.blob)); return urls.current.get(p.id)!; };

  useEffect(() => { loadDraft(uid).then((x) => { if (x?.draft) setD({ ...EMPTY, ...x.draft }); setLoaded(true); }).catch(() => setLoaded(true)); }, [uid]);
  useEffect(() => { if (online) api.get("/api/v1/mobile/tracks").then(setTracks).catch(() => undefined); }, [online]);
  // автосохранение черновика (вместе с фотографиями)
  useEffect(() => {
    if (!loaded) return;
    const t = setTimeout(() => { saveDraft(uid, d).catch(() => undefined); }, 400);
    return () => clearTimeout(t);
  }, [d, loaded]);
  const set = (p: Partial<Draft>) => { setD((s) => ({ ...s, ...p })); setChecked(false); };

  const search = async (num?: string) => {
    const v = (num ?? q).trim();
    if (v.length < 2) return;
    setErr(null);
    try { setFound(await api.get(`/api/v1/mobile/wagons?q=${encodeURIComponent(v)}`)); }
    catch { setFound(null); setErr("Поиск недоступен без связи. Укажите номер вручную — привязку выполнит оператор."); }
  };
  const pick = (w: any) => set({ wagon: w, manual: "", track_id: w.train?.track_id || w.track_id || "", position: w.position ? String(w.position) : "" });
  const addPhotos = async (fl: FileList | null) => {
    if (!fl) return;
    const out = [...d.photos];
    for (const f of Array.from(fl).slice(0, 6 - out.length)) out.push({ id: uuid(), blob: await compressPhoto(f), name: f.name.replace(/\.[^.]+$/, "") + ".jpg" });
    set({ photos: out });
  };

  const number = d.wagon?.number || d.manual.trim();
  const problems = [
    !number && "укажите вагон",
    number && !d.wagon && !/^\d{8}$/.test(number) && "номер вагона — 8 цифр",
    !d.category && "выберите категорию",
    d.description.trim().length < 3 && "опишите дефект",
  ].filter(Boolean) as string[];

  const send = async () => {
    if (problems.length || !checked) return;
    setBusy(true); setErr(null);
    try {
      let location = null;
      if (d.geo && navigator.geolocation) {
        location = await new Promise((res) => navigator.geolocation.getCurrentPosition(
          (p) => res({ lat: p.coords.latitude, lon: p.coords.longitude, accuracy_m: Math.round(p.coords.accuracy) }), () => res(null), { timeout: 5000 }));
      }
      const payload = {
        client_uuid: uuid(), wagon_id: d.wagon?.id ?? null, wagon_number: number, train_id: d.wagon?.train?.id ?? null,
        track_id: d.track_id || null, position: d.position ? Number(d.position) : null, category: d.category,
        component: d.component.trim() || null, description: d.description.trim(), urgency: d.urgency, location,
        client_created_at: new Date().toISOString(),
      };
      await enqueue(uid, payload, d.photos.map((p) => ({ id: p.id, name: p.name, type: p.blob.type, blob: p.blob })));
      await clearDraft(uid);
      setD(EMPTY);
      urls.current.forEach((u) => URL.revokeObjectURL(u)); urls.current.clear();
      useM.getState().say(online ? "Сообщение поставлено в отправку" : "Нет связи: сообщение сохранено и будет отправлено автоматически", online ? "ok" : "warn");
      syncNow(uid).catch(() => undefined);
      onSent();
    } catch (e: any) { setErr(e?.message || "Не удалось сохранить сообщение на устройстве"); }
    finally { setBusy(false); }
  };

  if (!loaded) return <div className="m-loading">Загрузка черновика…</div>;
  return (
    <div className="m-col">
      {qr && <QrScanner onClose={() => setQr(false)} onCode={(n) => { setQr(false); setQ(n); search(n); }} />}
      <section className="m-panel">
        <h3>1. Вагон</h3>
        {d.wagon ? (
          <div className="m-picked">
            <b>№ {d.wagon.number}</b> · {KIND_RU[d.wagon.kind] ?? d.wagon.kind}{d.wagon.loaded ? ", гружёный" : ""}
            <div>{d.wagon.train ? `Поезд № ${d.wagon.train.number}, ${d.wagon.train.track_label ?? "путь —"}, позиция ${d.wagon.position}` : d.wagon.track_label ?? "вне состава"}</div>
            <div className={d.wagon.condition === "ok" ? "m-muted" : "m-warn"}>Состояние: {d.wagon.condition_label}</div>
            <button className="m-btn ghost" onClick={() => set({ wagon: null })}>Выбрать другой</button>
          </div>
        ) : (
          <>
            <div className="m-row">
              <input value={q} onChange={(e) => setQ(e.target.value.replace(/\D/g, "").slice(0, 8))} inputMode="numeric" placeholder="Номер вагона" aria-label="Номер вагона"
                onKeyDown={(e) => e.key === "Enter" && search()} />
              <button className="m-btn" onClick={() => search()}>Найти</button>
            </div>
            <div className="m-row wrap">
              <button className="m-btn" onClick={() => api.get("/api/v1/mobile/trains").then(setTrains).catch(() => setErr("Список составов недоступен без связи."))}>Выбрать из состава</button>
              {"BarcodeDetector" in window ? <button className="m-btn" onClick={() => setQr(true)}>QR-код</button> : <small className="m-muted">QR-сканер не поддерживается этим браузером — используйте поиск.</small>}
            </div>
            {found && (found.length ? found.map((w) => (
              <button key={w.id} className="m-list-item" onClick={() => pick(w)}>
                <b>№ {w.number}</b><small>{KIND_RU[w.kind] ?? w.kind} · {w.train ? `поезд № ${w.train.number}, позиция ${w.position}` : w.track_label ?? "вне состава"} · {w.condition_label}</small>
              </button>
            )) : <div className="m-warn">Вагон не найден. Проверьте номер или укажите его вручную ниже — сообщение будет отправлено с неразрешённой привязкой.</div>)}
            {trains && trains.map((t) => (
              <details key={t.id} className="m-train">
                <summary>Поезд № {t.number} · {t.track_label ?? "на подходе"} · {t.wagons.length} ваг.</summary>
                <div className="m-wagon-grid">{t.wagons.map((w: any) => (
                  <button key={w.id} className={`m-wagon ${w.condition !== "ok" ? "bad" : ""}`} onClick={() => pick({ ...w, train: { id: t.id, number: t.number, track_id: t.track_id, track_label: t.track_label }, condition_label: w.condition === "ok" ? "Исправен" : "Есть ограничение" })}>
                    <small>{w.position}</small>{w.number.slice(-4)}</button>
                ))}</div>
              </details>
            ))}
            <label className="m-f">Ручной ввод (если вагон не найден)
              <input value={d.manual} onChange={(e) => set({ manual: e.target.value.replace(/\D/g, "").slice(0, 8) })} inputMode="numeric" placeholder="8 цифр" /></label>
          </>
        )}
        <div className="m-row">
          <label className="m-f grow">Путь
            <select value={d.track_id} onChange={(e) => set({ track_id: e.target.value })}>
              <option value="">— не указан —</option>
              {tracks.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select></label>
          <label className="m-f" style={{ width: 110 }}>Позиция<input value={d.position} onChange={(e) => set({ position: e.target.value.replace(/\D/g, "").slice(0, 3) })} inputMode="numeric" /></label>
        </div>
      </section>
      <section className="m-panel">
        <h3>2. Дефект</h3>
        <div className="m-choices">
          {Object.entries(ctx.dictionaries.categories).map(([k, v]) => (
            <button key={k} className={`m-choice ${d.category === k ? "on" : ""}`} aria-pressed={d.category === k} onClick={() => set({ category: k })}>{v}</button>
          ))}
        </div>
        <input value={d.component} onChange={(e) => set({ component: e.target.value })} placeholder="Узел / место (необязательно)" maxLength={120} />
        <textarea rows={4} value={d.description} onChange={(e) => set({ description: e.target.value })} placeholder="Что обнаружено" maxLength={4000} />
        <div className="m-urg">
          {Object.entries(ctx.dictionaries.urgency).map(([k, v]) => (
            <button key={k} className={`m-choice u-${k} ${d.urgency === k ? "on" : ""}`} aria-pressed={d.urgency === k} onClick={() => set({ urgency: k })}>{v}</button>
          ))}
        </div>
        {d.urgency === "critical" && <div className="m-warn">⛔ Для найденного вагона сразу вводится временное ограничение до проверки: вагон не отправляется со станцией, пока сообщение не рассмотрят.</div>}
      </section>
      <section className="m-panel">
        <h3>3. Фото ({d.photos.length}/6)</h3>
        <input ref={cam} type="file" accept="image/*" capture="environment" hidden onChange={(e) => { addPhotos(e.target.files); e.target.value = ""; }} />
        <input ref={gal} type="file" accept="image/*" multiple hidden onChange={(e) => { addPhotos(e.target.files); e.target.value = ""; }} />
        <div className="m-row"><button className="m-btn" disabled={d.photos.length >= 6} onClick={() => cam.current?.click()}>📷 Камера</button>
          <button className="m-btn" disabled={d.photos.length >= 6} onClick={() => gal.current?.click()}>🖼 Галерея</button></div>
        {d.photos.length > 0 && <div className="m-photos">{d.photos.map((p) => (
          <div key={p.id} className="m-photo-wrap"><img className="m-photo" src={purl(p)} alt="" />
            <button className="m-x" aria-label="Удалить фото" onClick={() => set({ photos: d.photos.filter((x) => x.id !== p.id) })}>✕</button></div>
        ))}</div>}
        <label className="m-check"><input type="checkbox" checked={d.geo} onChange={(e) => set({ geo: e.target.checked })} /> Приложить местоположение (с моего согласия)</label>
      </section>
      <section className="m-panel">
        <h3>4. Проверка и отправка</h3>
        <div className="m-kv">
          <span>Вагон</span><b>{number ? `№ ${number}${d.wagon ? "" : " (не найден, будет привязан позже)"}` : "—"}</b>
          <span>Категория</span><b>{ctx.dictionaries.categories[d.category] ?? "—"}</b>
          <span>Срочность</span><b>{ctx.dictionaries.urgency[d.urgency]}</b>
          <span>Фото</span><b>{d.photos.length}</b>
          <span>Автор и время</span><b>{ctx.user.full_name}, проставятся автоматически</b>
        </div>
        {problems.length > 0 && <div className="m-warn">Осталось: {problems.join(", ")}.</div>}
        <label className="m-check"><input type="checkbox" checked={checked} disabled={problems.length > 0} onChange={(e) => setChecked(e.target.checked)} /> Сведения проверены</label>
        {err && <div className="m-err">{err}</div>}
        <button className="m-btn primary big" disabled={busy || problems.length > 0 || !checked} onClick={send}>{busy ? "Сохранение…" : online ? "Отправить сообщение" : "Сохранить и отправить при связи"}</button>
        <small className="m-muted">Черновик сохраняется на этом устройстве автоматически.</small>
      </section>
    </div>
  );
}
