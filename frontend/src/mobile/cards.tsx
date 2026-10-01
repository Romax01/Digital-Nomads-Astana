// Карточки сообщения о дефекте и заявки на работы. Используются в мобильном разделе и в
// кабинете диспетчера. Доступность действий и причины недоступности приходят с сервера
// (allowed), решения выполняются только онлайн по актуальной версии записи (expected_version).
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { compressPhoto, fmtDT, ago, protectedImage, uploadPhoto, useM, uuid } from "./mlib";

export const DEFECT_CLS: Record<string, string> = { submitted: "warn", acknowledged: "info", under_review: "info", needs_info: "unknown",
  accepted: "ok", rejected: "muted", duplicate: "muted" };
export const WO_CLS: Record<string, string> = { created: "muted", assigned: "info", in_progress: "info", on_hold: "warn",
  awaiting_inspection: "warn", rework: "bad", completed: "ok", cancelled: "muted" };
export const URG_CLS: Record<string, string> = { normal: "muted", urgent: "warn", critical: "bad" };
export const URG_ICON: Record<string, string> = { normal: "○", urgent: "▲", critical: "⛔" };

export function Chip({ cls, children }: { cls: string; children: any }) {
  return <span className={`m-chip ${cls}`}>{children}</span>;
}

export function Photo({ url, alt }: { url: string; alt: string }) {
  const [src, setSrc] = useState<string | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => { protectedImage(url).then(setSrc).catch(() => setErr(true)); }, [url]);
  if (err) return <div className="m-photo m-photo-err">Фото недоступно</div>;
  return src ? <a href={src} target="_blank" rel="noreferrer"><img className="m-photo" src={src} alt={alt} /></a> : <div className="m-photo m-photo-load">…</div>;
}

function Timeline({ items }: { items: any[] }) {
  return (
    <ol className="m-timeline">
      {items.map((e, i) => (
        <li key={i} className={`k-${e.kind}`}>
          <div className="m-tl-h"><b>{e.user}</b><span>{fmtDT(e.at)}</span></div>
          <div>{e.text}</div>
        </li>
      ))}
    </ol>
  );
}

/** Кнопка действия: недоступна — с причиной текстом; без связи — объяснение, что действие только онлайн. */
export function Act({ label, why, onRun, kind = "", needText, placeholder, confirm }: {
  label: string; why?: string | null; onRun: (text: string) => Promise<any>; kind?: string; needText?: boolean; placeholder?: string; confirm?: string;
}) {
  const online = useM((s) => s.online);
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const reason = why ?? (online ? null : "Нет связи: решение выполняется только онлайн по актуальному состоянию.");
  const run = async () => {
    if (needText && !text.trim()) return;
    if (confirm && !window.confirm(confirm)) return;
    setBusy(true);
    try { await onRun(text.trim()); setOpen(false); setText(""); }
    catch (e) { const err = e as ApiError; useM.getState().say(err.message + (err.hint ? ` ${err.hint}` : ""), "error"); }
    finally { setBusy(false); }
  };
  if (reason) return <div className="m-act-dis"><button className={`m-btn ${kind}`} disabled>{label}</button><small>{reason}</small></div>;
  if (needText && open) {
    return (
      <div className="m-act-form">
        <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder={placeholder} rows={3} autoFocus />
        <div className="m-row">
          <button className={`m-btn ${kind}`} disabled={busy || !text.trim()} onClick={run}>{busy ? "…" : label}</button>
          <button className="m-btn ghost" onClick={() => setOpen(false)}>Отмена</button>
        </div>
      </div>
    );
  }
  return <button className={`m-btn ${kind}`} disabled={busy} onClick={() => (needText ? setOpen(true) : run())}>{busy ? "…" : label}</button>;
}

function WagonLine({ w, raw }: { w: any; raw?: string }) {
  if (!w) return <div className="m-warn">⚠ Вагон № {raw} не найден в системе — сообщение ожидает привязки уполномоченным сотрудником.</div>;
  return (
    <div className="m-kv">
      <span>Вагон</span><b>№ {w.number} · {({ gondola: "полувагон", covered: "крытый", tank: "цистерна", flat: "платформа", hopper: "хоппер", passenger: "пассажирский" } as any)[w.kind] ?? w.kind}{w.loaded ? ", гружёный" : ", порожний"}</b>
      <span>Состояние</span><b className={w.condition === "ok" ? "ok" : "bad"}>{w.condition_label}</b>
      <span>Местонахождение</span><b>{w.train ? `поезд № ${w.train.number}, позиция ${w.position}` : w.track_id ? "вне состава (на пути станции)" : "—"}</b>
    </div>
  );
}

/** Подбор исполнителя (старший осмотрщик — своей зоны, диспетчер — станции). */
function AssigneeSelect({ kind, value, onChange }: { kind?: string; value: string; onChange: (v: string) => void }) {
  const [list, setList] = useState<any[]>([]);
  useEffect(() => { api.get(`/api/v1/work-orders/executors${kind ? `?kind=${kind}` : ""}`).then(setList).catch(() => setList([])); }, [kind]);
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">— без назначения —</option>
      {list.map((u) => <option key={u.id} value={u.id}>{u.name} · {u.role_label}{u.brigade_id ? ` · ${u.brigade_id}` : ""} · заданий: {u.active_tasks}</option>)}
    </select>
  );
}

function DecisionForm({ d, onDone }: { d: any; onDone: () => void }) {
  const kinds = useM((s) => s.ctx?.dictionaries.work_kinds) ?? { inspection: "Дополнительный осмотр", repair_in_place: "Ремонт без отцепки",
    uncoupling_repair: "Отцепка и ремонт", replacement: "Замена вагона в составе", transfer: "Передача на другую ремонтную площадку" };
  const [kind, setKind] = useState("repair_in_place");
  const [assignee, setAssignee] = useState("");
  const [reason, setReason] = useState("");
  const [cands, setCands] = useState<any>(null);
  const [repl, setRepl] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => {
    if (kind === "replacement" && d.wagon) api.get(`/api/v1/wagons/${d.wagon.id}/replacement-candidates`).then(setCands).catch((e) => setErr(e));
  }, [kind]);
  const submit = async () => {
    setBusy(true); setErr(null);
    try {
      await api.post(`/api/v1/defect-reports/${d.id}/${d.add ? "work-orders" : "decision"}`, { kind, reason: reason || null, assignee_id: assignee || null,
        expected_version: d.version, replacement_wagon_id: kind === "replacement" ? repl || null : null }, uuid());
      useM.getState().say("Решение принято, заявка на работы создана");
      onDone();
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  return (
    <div className="m-panel">
      <h3>{d.add ? "Дополнительная заявка на работы" : "Решение по сообщению"}</h3>
      <div className="m-choices">
        {Object.entries(kinds).map(([k, v]) => (
          <button key={k} className={`m-choice ${kind === k ? "on" : ""}`} onClick={() => setKind(k)} aria-pressed={kind === k}>{v as string}</button>
        ))}
      </div>
      {kind === "replacement" && cands && (
        <div className="m-col">
          {cands.problems.map((p: string, i: number) => <div key={i} className="m-warn">{p}</div>)}
          <label className="m-f">Исправный вагон для замены
            <select value={repl} onChange={(e) => setRepl(e.target.value)}>
              <option value="">— выберите —</option>
              {cands.candidates.map((c: any) => (
                <option key={c.id} value={c.id} disabled={!c.suitable}>№ {c.number} · {c.kind}{c.suitable ? "" : ` — ${c.reasons.join(", ")}`}</option>
              ))}
            </select></label>
          <small className="m-muted">Состав изменится только после выполнения операции отцепки, подачи и прицепки. Снятый вагон останется неисправным до своего ремонта.</small>
        </div>
      )}
      <label className="m-f">Исполнитель<AssigneeSelect kind={kind} value={assignee} onChange={setAssignee} /></label>
      <label className="m-f">Основание / указания<textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
      {err && <div className="m-err">{err.message}{err.hint ? <div><small>{err.hint}</small></div> : null}
        {err.details?.options && <div><small>Возможные варианты: {err.details.options.map((o: string) => kinds[o as keyof typeof kinds]).join(", ")}</small></div>}</div>}
      <button className="m-btn primary" disabled={busy || (kind === "replacement" && !repl)} onClick={submit}>{busy ? "…" : d.add ? "Создать заявку" : "Принять и создать заявку"}</button>
    </div>
  );
}

function PhotoPicker({ onAdd, label = "Добавить фото" }: { onAdd: (files: { id: string; blob: Blob; name: string }[]) => void; label?: string }) {
  const cam = useRef<HTMLInputElement>(null);
  const gal = useRef<HTMLInputElement>(null);
  const take = async (fl: FileList | null) => {
    if (!fl) return;
    const out = [];
    for (const f of Array.from(fl).slice(0, 6)) out.push({ id: uuid(), blob: await compressPhoto(f), name: f.name.replace(/\.[^.]+$/, "") + ".jpg" });
    onAdd(out);
  };
  return (
    <div className="m-row">
      <input ref={cam} type="file" accept="image/*" capture="environment" hidden onChange={(e) => { take(e.target.files); e.target.value = ""; }} />
      <input ref={gal} type="file" accept="image/*" multiple hidden onChange={(e) => { take(e.target.files); e.target.value = ""; }} />
      <button className="m-btn" type="button" onClick={() => cam.current?.click()}>📷 Камера</button>
      <button className="m-btn" type="button" onClick={() => gal.current?.click()}>🖼 Галерея</button>
      <span className="m-muted">{label}</span>
    </div>
  );
}

/** Онлайн-загрузка фото для отчёта/ответа (не офлайн-очередь). */
function useOnlinePhotos() {
  const [items, setItems] = useState<{ id: string; blob: Blob; name: string; attId?: string; url: string }[]>([]);
  const add = (files: { id: string; blob: Blob; name: string }[]) => setItems((s) => [...s, ...files.map((f) => ({ ...f, url: URL.createObjectURL(f.blob) }))]);
  const upload = async () => {
    const ids: string[] = [];
    for (const it of items) { it.attId = it.attId ?? await uploadPhoto(it.blob, it.id, it.name); ids.push(it.attId); }
    return ids;
  };
  const clear = () => { items.forEach((i) => URL.revokeObjectURL(i.url)); setItems([]); };
  return { items, add, upload, clear, remove: (id: string) => setItems((s) => s.filter((x) => x.id !== id)) };
}

export function DefectCard({ id, onOpenWork, desktopExtra }: { id: string; onOpenWork: (wid: string) => void; desktopExtra?: (d: any) => any }) {
  const seq = useM((s) => s.seq);
  const [d, setD] = useState<any>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [decide, setDecide] = useState(false);
  const [linkQ, setLinkQ] = useState("");
  const [found, setFound] = useState<any[]>([]);
  const [dupOf, setDupOf] = useState("");
  const reply = useOnlinePhotos();
  const load = () => api.get(`/api/v1/defect-reports/${id}`).then((x) => { setD(x); setErr(null); }).catch(setErr);
  useEffect(() => { load(); }, [id, seq]);
  if (err) return <div className="m-err">{err.message}</div>;
  if (!d) return <div className="m-loading">Загрузка…</div>;
  const a = d.allowed || {};
  const act = (action: string, body: any = {}) => api.post(`/api/v1/defect-reports/${d.id}/actions/${action}`, { expected_version: d.version, ...body }, uuid())
    .then((x) => { setD({ ...d, ...x }); load(); });
  return (
    <div className="m-card">
      <div className="m-card-h">
        <h2>Сообщение № {d.number}</h2>
        <Chip cls={DEFECT_CLS[d.status]}>{d.status_label}</Chip>
      </div>
      <div className="m-row wrap">
        <Chip cls={URG_CLS[d.urgency]}>{URG_ICON[d.urgency]} {d.urgency_label}</Chip>
        {d.restriction_active && <Chip cls="bad">⛔ Ограничение до проверки: вагон не отправляется</Chip>}
        {d.fault_open && <Chip cls="bad">Неисправность подтверждена, не устранена</Chip>}
      </div>
      <WagonLine w={d.wagon} raw={d.wagon_number} />
      <div className="m-kv">
        <span>Категория</span><b>{d.category_label}{d.component ? ` · ${d.component}` : ""}</b>
        <span>Автор</span><b>{d.author?.name}</b>
        <span>Доставлено на сервер</span><b>{fmtDT(d.delivered_at)}</b>
        <span>Получение подтверждено</span><b className={d.acknowledged_at ? "ok" : "warn"}>{d.acknowledged_at ? `${fmtDT(d.acknowledged_at)} · ${d.acknowledged_by}` : `ещё нет${d.waiting_min != null ? ` (ожидает ${d.waiting_min} мин)` : ""}`}</b>
        {d.decision_label && <><span>Решение</span><b>{d.decision_label}{d.decision_reason ? ` — ${d.decision_reason}` : ""}</b></>}
        {d.status === "rejected" && <><span>Основание отказа</span><b>{d.decision_reason}</b></>}
        {d.duplicate_of && <><span>Дубликат</span><b>сообщения № {d.duplicate_of_number}</b></>}
      </div>
      <p className="m-desc">{d.description}</p>
      {d.attachments?.length > 0 && <div className="m-photos">{d.attachments.map((p: any) => <Photo key={p.id} url={p.url} alt={p.name} />)}</div>}
      {desktopExtra?.(d)}
      {d.work_orders?.length > 0 && (
        <div className="m-col">
          <h3>Связанные работы</h3>
          {d.work_orders.map((w: any) => (
            <button key={w.id} className="m-list-item" onClick={() => onOpenWork(w.id)}>
              <b>№ {w.number} · {w.kind_label}</b><Chip cls={WO_CLS[w.status]}>{w.status_label}</Chip>
              <small>{w.assignee ? `Исполнитель: ${w.assignee.name}` : "Исполнитель не назначен"}{w.overdue ? " · ⚠ просрочено" : ""}</small>
            </button>
          ))}
        </div>
      )}
      <div className="m-actions">
        {d.status === "needs_info" && a.reply === null && (
          <div className="m-panel">
            <h3>Ответ на уточнение</h3>
            <PhotoPicker onAdd={reply.add} />
            {reply.items.length > 0 && <div className="m-photos">{reply.items.map((p) => <img key={p.id} className="m-photo" src={p.url} alt="" />)}</div>}
            <Act label="Отправить ответ" needText placeholder="Что уточнено" onRun={async (t) => { const ids = await reply.upload(); await act("reply", { reason: t, attachment_ids: ids }); reply.clear(); }} />
          </div>
        )}
        {a.acknowledge !== undefined && d.status === "submitted" && <Act label="✓ Подтвердить получение" kind="primary" why={a.acknowledge} onRun={() => act("acknowledge")} />}
        {a.review !== undefined && ["submitted", "acknowledged", "needs_info"].includes(d.status) && <Act label="Взять на рассмотрение" why={a.review} onRun={(t) => act("review", { reason: t })} />}
        {a.needs_info !== undefined && ["submitted", "acknowledged", "under_review"].includes(d.status) && <Act label="Запросить уточнение" why={a.needs_info} needText placeholder="Что нужно уточнить" onRun={(t) => act("needs_info", { reason: t })} />}
        {a.link_wagon === null && (!d.wagon || d.status !== "accepted") && ["submitted", "acknowledged", "under_review", "needs_info"].includes(d.status) && (
          <div className="m-panel">
            <h3>{d.wagon ? "Перепривязать к вагону" : "Привязать к вагону"}</h3>
            <div className="m-row"><input value={linkQ} onChange={(e) => setLinkQ(e.target.value)} placeholder="Номер вагона" inputMode="numeric" />
              <button className="m-btn" onClick={() => linkQ.trim().length >= 2 && api.get(`/api/v1/mobile/wagons?q=${encodeURIComponent(linkQ.trim())}`).then(setFound)}>Найти</button></div>
            {found.map((w) => <Act key={w.id} label={`№ ${w.number}${w.train ? ` · поезд № ${w.train.number}, поз. ${w.position}` : ""}`} onRun={() => act("link_wagon", { wagon_id: w.id }).then(() => setFound([]))} />)}
          </div>
        )}
        {a.decide !== undefined && ["acknowledged", "under_review"].includes(d.status) && (a.decide === null
          ? (decide ? <DecisionForm d={d} onDone={() => { setDecide(false); load(); }} /> : <button className="m-btn primary" onClick={() => setDecide(true)}>Принять решение…</button>)
          : <div className="m-act-dis"><button className="m-btn" disabled>Принять решение</button><small>{a.decide}</small></div>)}
        {a.add_work_order === null && (decide ? <DecisionForm d={{ ...d, add: true }} onDone={() => { setDecide(false); load(); }} />
          : <button className="m-btn" onClick={() => setDecide(true)}>Дополнительная заявка (например, ремонт снятого вагона)…</button>)}
        {a.reject !== undefined && ["submitted", "acknowledged", "under_review", "needs_info"].includes(d.status) && <Act label="Отклонить" kind="danger" why={a.reject} needText placeholder="Основание отклонения (обязательно)" onRun={(t) => act("reject", { reason: t })} />}
        {a.duplicate === null && ["submitted", "acknowledged", "under_review", "needs_info"].includes(d.status) && (
          <div className="m-panel">
            <h3>Объединить как дубликат</h3>
            <input value={dupOf} onChange={(e) => setDupOf(e.target.value)} placeholder="ID исходного сообщения (DR-…)" />
            <Act label="Объединить" why={dupOf ? null : "Укажите исходное сообщение."} needText placeholder="Основание объединения" onRun={(t) => act("duplicate", { duplicate_of: dupOf.trim(), reason: t })} />
          </div>
        )}
        <Act label="Добавить замечание" kind="ghost" needText placeholder="Замечание" onRun={(t) => act("comment", { reason: t })} />
      </div>
      <h3>Хронология</h3>
      <Timeline items={d.timeline || []} />
    </div>
  );
}

export function WorkCard({ id, onOpenDefect, desktopExtra }: { id: string; onOpenDefect: (rid: string) => void; desktopExtra?: (w: any) => any }) {
  const seq = useM((s) => s.seq);
  const [w, setW] = useState<any>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [report, setReport] = useState("");
  const [materials, setMaterials] = useState("");
  const [assignee, setAssignee] = useState("");
  const photos = useOnlinePhotos();
  const load = () => api.get(`/api/v1/work-orders/${id}`).then((x) => { setW(x); setErr(null); }).catch(setErr);
  useEffect(() => { load(); }, [id, seq]);
  if (err) return <div className="m-err">{err.message}</div>;
  if (!w) return <div className="m-loading">Загрузка…</div>;
  const a = w.allowed || {};
  const act = (action: string, body: any = {}) => api.post(`/api/v1/work-orders/${w.id}/actions/${action}`, { expected_version: w.version, ...body }, uuid()).then(() => load());
  return (
    <div className="m-card">
      <div className="m-card-h"><h2>Задание № {w.number}</h2><Chip cls={WO_CLS[w.status]}>{w.status_label}</Chip></div>
      <div className="m-row wrap"><Chip cls="info">{w.kind_label}</Chip>{w.overdue && <Chip cls="bad">⚠ Срок истёк</Chip>}</div>
      <WagonLine w={w.wagon} />
      <div className="m-kv">
        <span>Ответственный</span><b>{w.assignee?.name ?? "не назначен"}{w.brigade_id ? ` · бригада ${w.brigade_id}` : ""}</b>
        <span>Согласованный срок</span><b className={w.overdue ? "bad" : ""}>{fmtDT(w.due_at)}</b>
        {w.replacement_wagon && <><span>Вагон для замены</span><b>№ {w.replacement_wagon.number} ({w.replacement_wagon.condition_label})</b></>}
        <span>Место работ</span><b className={w.place_ready ? "ok" : "warn"}>{w.place_ready ? "готово" : w.place_reason}</b>
        {w.hold_reason && <><span>Приостановлено</span><b>{w.hold_reason}</b></>}
        {w.started_at && <><span>Начато</span><b>{fmtDT(w.started_at)}</b></>}
        {w.submitted_at && <><span>Передано на осмотр</span><b>{fmtDT(w.submitted_at)}</b></>}
        {w.completed_at && <><span>Принято</span><b>{fmtDT(w.completed_at)}</b></>}
      </div>
      {w.defect && <button className="m-list-item" onClick={() => onOpenDefect(w.defect.id)}><b>Сообщение № {w.defect.number}</b><small>{w.defect.category_label}: {w.defect.description.slice(0, 90)}</small></button>}
      {w.actions?.length > 0 && <><h3>Необходимые действия</h3><ul>{w.actions.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul></>}
      {w.operations?.length > 0 && <><h3>Операции по составу</h3>{w.operations.map((o: any) => <div key={o.id} className="m-op"><b>{o.note}</b><small>{o.status === "done" ? "выполнена" : o.status === "in_progress" ? "выполняется" : o.reserved ? `запланирована на ${fmtDT(o.planned_start)}` : "ожидает планирования"}</small></div>)}</>}
      {w.report && <><h3>Отчёт исполнителя</h3><p className="m-desc">{w.report}{w.materials ? <><br /><small>Материалы: {w.materials}</small></> : null}</p></>}
      {w.attachments?.length > 0 && <div className="m-photos">{w.attachments.map((p: any) => <Photo key={p.id} url={p.url} alt={p.name} />)}</div>}
      {w.inspections?.length > 0 && <><h3>Контрольный осмотр</h3>{w.inspections.map((i: any, k: number) => <div key={k} className={`m-op ${i.result === "accepted" ? "ok" : "bad"}`}><b>{i.result === "accepted" ? "✓ Принято" : "↺ На доработку"} · {i.inspector}</b><small>{fmtDT(i.at)}{i.comment ? ` — ${i.comment}` : ""}</small></div>)}</>}
      {desktopExtra?.(w)}
      <div className="m-actions">
        {(w.status === "assigned" || w.status === "rework") && <Act label={w.status === "rework" ? "Начать доработку" : "▶ Начать работы"} kind="primary" why={a.start} onRun={() => act("start")} />}
        {w.status === "on_hold" && <Act label="Возобновить" kind="primary" why={a.resume} onRun={() => act("resume")} />}
        {(w.status === "assigned" || w.status === "in_progress") && <Act label="⏸ Приостановить" why={a.pause} needText placeholder="Причина приостановки" onRun={(t) => act("pause", { reason: t })} />}
        {w.status === "in_progress" && (a.submit === null ? (
          <div className="m-panel">
            <h3>Отчёт о выполнении</h3>
            <textarea rows={3} value={report} onChange={(e) => setReport(e.target.value)} placeholder="Что сделано" />
            <input value={materials} onChange={(e) => setMaterials(e.target.value)} placeholder="Материалы (необязательно)" />
            <PhotoPicker onAdd={photos.add} label="фото результата" />
            {photos.items.length > 0 && <div className="m-photos">{photos.items.map((p) => <img key={p.id} className="m-photo" src={p.url} alt="" onClick={() => photos.remove(p.id)} />)}</div>}
            <Act label="Передать на контрольный осмотр" kind="primary" why={report.trim() ? null : "Опишите выполненные работы."}
              onRun={async () => { const ids = await photos.upload(); await act("submit", { report, materials, attachment_ids: ids }); photos.clear(); setReport(""); }} />
          </div>) : <div className="m-act-dis"><button className="m-btn" disabled>Передать результат</button><small>{a.submit}</small></div>)}
        {w.status === "awaiting_inspection" && <>
          <Act label="✓ Принять результат" kind="primary" why={a.accept} onRun={(t) => act("accept", { comment: t })} confirm="Подтвердить приёмку по контрольному осмотру?" />
          <Act label="↺ Вернуть на доработку" kind="danger" why={a.rework} needText placeholder="Замечания для доработки" onRun={(t) => act("rework", { reason: t })} />
        </>}
        {a.assign === null && (
          <div className="m-panel">
            <h3>{w.assignee ? "Переназначить" : "Назначить исполнителя"}</h3>
            <AssigneeSelect kind={w.kind} value={assignee} onChange={setAssignee} />
            <Act label="Назначить" why={assignee ? null : "Выберите исполнителя."} onRun={() => act("assign", { assignee_id: assignee })} />
          </div>
        )}
        {a.cancel === null && <Act label="Отменить заявку" kind="danger" needText placeholder="Основание отмены" onRun={(t) => act("cancel", { reason: t })} />}
        <Act label="Добавить замечание" kind="ghost" needText placeholder="Замечание" onRun={(t) => act("comment", { reason: t })} />
      </div>
      <h3>Ход выполнения</h3>
      <Timeline items={w.timeline || []} />
      <small className="m-muted">Обновлено {ago(w.updated_at)}</small>
    </div>
  );
}
