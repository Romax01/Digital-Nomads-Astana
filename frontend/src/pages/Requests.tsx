import { useEffect, useState } from "react";
import { api, ApiError, newKey } from "../lib/api";
import { fmtFull, fmtHM, fmtHMS, fmtMin } from "../lib/format";
import { DECISION, ITEM_STATUS, OP_LABEL, REQ_STATUS_CLS } from "../lib/labels";
import { can, useStore, useViewState } from "../lib/store";
import { ActionButton, Badge, Empty, ErrorBox, Loading, Modal, notifyError, useFetch } from "../components/ui";

const WAGON_KINDS: Record<string, string> = { gondola: "Полувагоны (13,92 м)", covered: "Крытые (17,64 м)", tank: "Цистерны (12,02 м)", flat: "Платформы (14,62 м)", hopper: "Хопперы (14,72 м)" };
const toLocal = (iso: string) => new Date(new Date(iso).getTime() + 5 * 3600e3).toISOString().slice(0, 16);
const fromLocal = (v: string) => new Date(v + ":00+05:00").toISOString();

export default function Requests() {
  const live = useViewState();
  const user = useStore((s) => s.user);
  const reqKey = live ? Object.values(live.requests).map((r: any) => r.updated_at + r.status).join("|") : "";
  const list = useFetch(() => api.get("/api/v1/requests"), [live?.meta.state_version, reqKey]);
  const stations = useFetch(() => api.get("/api/v1/stations"), []);
  const [selId, setSelId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  useEffect(() => { if (!selId && list.data?.length) setSelId(list.data[0].id); }, [list.data]);
  const neighbors = (stations.data ?? []).filter((s: any) => s.kind === "neighbor");
  const name = (id: string) => (stations.data ?? []).find((s: any) => s.id === id)?.name ?? id;
  return (
    <div className="page">
      <div className="row between wrap">
        <h1>Заявки между станциями</h1>
        <ActionButton kind="primary" disabledReason={can(user, "request.create") ? null : "Создание заявок — поездной или станционный диспетчер."}
          onClick={() => setCreating(true)}>＋ Создать заявку</ActionButton>
      </div>
      <div className="grid2" style={{ gridTemplateColumns: "minmax(340px, 1fr) minmax(0, 2.2fr)" }}>
        <section className="panel" aria-label="Список заявок">
          {list.loading && !list.data ? <Loading /> : list.error ? <ErrorBox error={list.error} retry={list.reload} /> :
            !list.data?.length ? <Empty text="Заявок нет" hint="Создайте заявку на отправление состава на станцию." /> : (
              <table className="t">
                <thead><tr><th>№</th><th>Маршрут</th><th>Ваг.</th><th>Отпр.</th><th>Статус</th></tr></thead>
                <tbody>{list.data.map((r: any) => (
                  <tr key={r.id} className={`clickable ${r.id === selId ? "sel" : ""}`} onClick={() => setSelId(r.id)} tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && setSelId(r.id)} aria-selected={r.id === selId}>
                    <td><b>{r.number}</b></td><td>{name(r.from_station_id)} → {name(r.to_station_id)}</td><td>{r.wagons_count}</td>
                    <td className="nowrap">{fmtHM(r.desired_departure)}</td>
                    <td><Badge cls={REQ_STATUS_CLS[r.status] ?? "muted"}>{r.status_label}</Badge>
                      {r.last_check?.decision && <div><Badge cls={DECISION[r.last_check.decision].cls} icon={DECISION[r.last_check.decision].icon}>{DECISION[r.last_check.decision].label}</Badge></div>}</td>
                  </tr>))}
                </tbody>
              </table>)}
        </section>
        <section className="panel" aria-label="Заявка">
          {selId ? <RequestDetail id={selId} name={name} onChanged={list.reload} /> : <Empty text="Выберите заявку" />}
        </section>
      </div>
      {creating && <CreateDialog neighbors={neighbors} onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); list.reload(); setSelId(id); }} />}
    </div>
  );
}

function CreateDialog({ neighbors, onClose, onCreated }: { neighbors: any[]; onClose: () => void; onCreated: (id: string) => void }) {
  const st = useViewState();
  const def = st ? toLocal(new Date(new Date(st.meta.model_time).getTime() + 60 * 60e3).toISOString()) : "";
  const [f, setF] = useState<any>({ from_station_id: neighbors[0]?.id ?? "OTR", wagons_count: 40, wagon_kind: "gondola", train_length_m: "", cargo: "", priority: 2, split_allowed: true, dep: def });
  const [err, setErr] = useState<ApiError | null>(null);
  const key = useState(newKey())[0];
  const submit = async () => {
    try {
      const r = await api.post("/api/v1/requests", { from_station_id: f.from_station_id, wagons_count: +f.wagons_count,
        wagon_kind: f.wagon_kind || null, train_length_m: f.train_length_m ? +f.train_length_m : null, cargo: f.cargo || null,
        priority: +f.priority, split_allowed: !!f.split_allowed, desired_departure: fromLocal(f.dep) }, key);
      useStore.getState().toast("ok", `Заявка ${r.number} создана`, "Выполните «Проверить приём».");
      onCreated(r.id);
    } catch (e) { setErr(e as ApiError); }
  };
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  return (
    <Modal title="Новая заявка на отправление" onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Отмена</button><button className="btn primary" onClick={submit}>Создать</button></>}>
      <div className="form-grid">
        <label className="f">Станция отправления<select value={f.from_station_id} onChange={set("from_station_id")}>{neighbors.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}</select></label>
        <label className="f">Вагонов, шт.<input type="number" min={1} max={120} value={f.wagons_count} onChange={set("wagons_count")} /></label>
        <label className="f">Род вагонов<select value={f.wagon_kind} onChange={set("wagon_kind")}><option value="">не указан</option>{Object.entries(WAGON_KINDS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label className="f">Длина состава, м (необязательно)<input type="number" min={1} value={f.train_length_m} onChange={set("train_length_m")} placeholder="рассчитается по роду вагонов" /></label>
        <label className="f">Отправление (время станции, UTC+5)<input type="datetime-local" value={f.dep} onChange={set("dep")} /></label>
        <label className="f">Приоритет (1–5)<input type="number" min={1} max={5} value={f.priority} onChange={set("priority")} /></label>
        <label className="f">Груз<input value={f.cargo} onChange={set("cargo")} /></label>
        <label className="f row" style={{ flexDirection: "row", alignItems: "center" }}><input type="checkbox" checked={f.split_allowed} onChange={set("split_allowed")} /> Разрешено разделение партии</label>
      </div>
      <p className="faint" style={{ margin: 0 }}>Количество вагонов не заменяет длину: без длины состава и рода вагонов проверка вернёт «Недостаточно данных».</p>
      {err && <ErrorBox error={err} />}
      {err?.details?.fields && <ul>{err.details.fields.map((x: any, i: number) => <li key={i}>{x.field}: {x.message}</li>)}</ul>}
    </Modal>
  );
}

function RequestDetail({ id, name, onChanged }: { id: string; name: (id: string) => string; onChanged: () => void }) {
  const live = useViewState();
  const user = useStore((s) => s.user);
  const mode = useStore((s) => s.mode);
  const r = useFetch(() => api.get(`/api/v1/requests/${id}`), [id, live?.requests?.[id]?.updated_at, live?.meta.state_version]);
  const [checking, setChecking] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [moveOpen, setMoveOpen] = useState(false);
  const [reasonOpen, setReasonOpen] = useState<null | "cancel" | "reject">(null);
  const [lastErr, setLastErr] = useState<ApiError | null>(null);
  if (r.loading && !r.data) return <Loading />;
  if (r.error) return <ErrorBox error={r.error} retry={r.reload} />;
  const q = r.data;
  const chk = q.last_check;
  const ro = mode === "history" ? "В режиме «История» команды недоступны." : null;
  const check = async () => {
    setChecking(true); setLastErr(null);
    try { await api.postPlain(`/api/v1/requests/${id}/check`); r.reload(); onChanged(); } catch (e) { notifyError(e); } finally { setChecking(false); }
  };
  const confirmReason = ro ?? (!can(user, "request.confirm") ? "Подтверждение — роль «Дежурный по станции»." :
    q.status === "new" ? "Сначала выполните «Проверить приём»." : q.status !== "checked" ? `Заявка в статусе «${q.status_label}».` :
      q.check_stale ? "Проверка устарела: состояние станции изменилось. Выполните проверку повторно." :
        chk?.decision === "unavailable" ? "Приём недоступен — выберите проверенную альтернативу." :
          chk?.decision === "insufficient_data" ? "Недостаточно данных — дополните заявку." : null);
  const checkReason = ro ?? (!can(user, "request.check") ? "Проверка доступна диспетчерам и дежурному." :
    !["new", "checked", "confirmed"].includes(q.status) ? `Заявка в статусе «${q.status_label}».` : null);
  return (
    <div>
      <div className="panel-h wrap">
        <h2 className="grow">Заявка {q.number}: {name(q.from_station_id)} → {name(q.to_station_id)}</h2>
        <Badge cls={REQ_STATUS_CLS[q.status] ?? "muted"}>{q.status_label}</Badge>
      </div>
      <div className="panel-b col">
        <dl className="kv">
          <dt>Состав</dt><dd>{q.wagons_count} ваг. · {q.wagon_kind ? WAGON_KINDS[q.wagon_kind] : "род не указан"}{q.train_length_m ? ` · ${q.train_length_m} м` : ""}{q.cargo ? ` · ${q.cargo}` : ""}</dd>
          <dt>Отправление</dt><dd>{fmtFull(q.desired_departure)}{chk?.arrival ? ` → прибытие ${fmtHM(chk.arrival)} (ход ${chk.travel_min} мин)` : ""}</dd>
          <dt>Приоритет</dt><dd>{q.priority} · разделение {q.split_allowed ? "разрешено" : "не разрешено"}</dd>
          {q.decision_reason && <><dt>Основание</dt><dd>{q.decision_reason}</dd></>}
        </dl>
        <div className="row wrap">
          <ActionButton kind="primary" busy={checking} disabledReason={checkReason} onClick={check}>Проверить приём</ActionButton>
          <ActionButton kind="primary" disabledReason={confirmReason} onClick={() => setConfirmOpen(true)}>Подтвердить заявку</ActionButton>
          <ActionButton disabledReason={ro ?? (!can(user, "request.reschedule") ? "Перенос — поездной диспетчер или дежурный." : !["new", "checked", "confirmed"].includes(q.status) ? "Статус не допускает перенос." : null)}
            onClick={() => setMoveOpen(true)}>Перенести отправление</ActionButton>
          <ActionButton kind="danger" disabledReason={ro ?? (!can(user, "request.cancel") ? "Отмена — поездной диспетчер или дежурный." : !["new", "checked", "confirmed"].includes(q.status) ? "Статус не допускает отмену." : null)}
            onClick={() => setReasonOpen("cancel")}>Отменить</ActionButton>
          <ActionButton kind="danger" disabledReason={ro ?? (!can(user, "request.reject") ? "Отказ — дежурный по станции." : q.status !== "checked" ? "Отказ возможен после проверки." : null)}
            onClick={() => setReasonOpen("reject")}>Отказать</ActionButton>
        </div>
        {lastErr && <ErrorBox error={lastErr} />}
        {!chk ? <Empty text="Проверка ещё не выполнялась" hint="Нажмите «Проверить приём»: сервер проверит план, вместимость, пропускную способность, пути, маршруты, ресурсы, соседнюю станцию и данные датчиков." /> :
          <CheckResult chk={chk} stale={q.check_stale} request={q} onApplied={() => { r.reload(); onChanged(); }} />}
      </div>
      {confirmOpen && chk && <ConfirmDialog q={q} chk={chk} onClose={() => setConfirmOpen(false)} onDone={() => { setConfirmOpen(false); r.reload(); onChanged(); }} onError={(e) => { setLastErr(e); r.reload(); }} />}
      {moveOpen && <MoveDialog q={q} onClose={() => setMoveOpen(false)} onDone={() => { setMoveOpen(false); r.reload(); onChanged(); }} />}
      {reasonOpen && <ReasonDialog q={q} kind={reasonOpen} onClose={() => setReasonOpen(null)} onDone={() => { setReasonOpen(null); r.reload(); onChanged(); }} />}
    </div>
  );
}

function CheckResult({ chk, stale, request, onApplied }: { chk: any; stale: boolean; request: any; onApplied: () => void }) {
  const user = useStore((s) => s.user);
  const d = DECISION[chk.decision];
  return (
    <div className="col">
      <div className={`callout ${d.cls === "ok" ? "ok" : d.cls === "warn" ? "warn" : d.cls === "bad" ? "bad" : "unknown"}`} role="status">
        <div className="row"><Badge cls={d.cls} icon={d.icon}>{d.label}</Badge></div>
        <p style={{ margin: "6px 0 0", fontSize: 15 }}><b>{chk.summary}</b></p>
        <div className="faint">Расчёт: {fmtHMS(chk.computed_at)} модельного времени · версия состояния {chk.state_version} · {chk.calc_ms} мс</div>
        {stale && <div className="warn" style={{ color: "var(--warn)" }}>⏱ Проверка устарела — состояние станции изменилось. Перед подтверждением сервер проверит заново.</div>}
      </div>
      {chk.missing_data?.length > 0 && <div className="callout unknown"><b>? Недостаточно данных:</b><ul>{chk.missing_data.map((m: string, i: number) => <li key={i}>{m}</li>)}</ul></div>}
      <h3>Ограничения</h3>
      <table className="t">
        <thead><tr><th>Ограничение</th><th>Результат</th><th>Пояснение</th></tr></thead>
        <tbody>{chk.items.map((i: any) => (
          <tr key={i.code}>
            <td><b>{i.title}</b>{i.unit && <div className="faint">{i.unit} · {i.period}</div>}</td>
            <td><Badge cls={ITEM_STATUS[i.status].cls} icon={ITEM_STATUS[i.status].icon}>{ITEM_STATUS[i.status].label}</Badge></td>
            <td>{i.message}{(i.rule || i.source) && <details><summary>Правило и источник</summary><div className="muted">{i.rule}</div><div className="faint">Источник: {i.source}</div></details>}</td>
          </tr>))}</tbody>
      </table>
      {chk.tracks?.length > 0 && (
        <details open={chk.decision !== "available"}>
          <summary>Почему не подходят другие пути ({chk.tracks.length})</summary>
          <ul>{chk.tracks.map((t: any) => <li key={t.track_id}><Badge cls={t.code === "LENGTH" ? "muted" : t.code === "DATA" ? "unknown" : "bad"}>{{ LENGTH: "длина", OCCUPIED: "занят", CLOSED: "закрыт", DATA: "нет данных", RESOURCE: "ресурсы", ROUTE: "маршрут", INCOMPATIBLE: "не подходит", INSUFFICIENT_DATA: "нет данных" }[t.code as string] ?? t.code}</Badge> {t.message}</li>)}</ul>
        </details>
      )}
      {chk.assignment && (
        <><h3>План приёма и обработки</h3>
          <table className="t"><thead><tr><th>Операция</th><th>Путь</th><th>Время</th><th>Ресурсы</th></tr></thead>
            <tbody>{chk.assignment.ops.map((o: any) => (
              <tr key={o.seq}><td>{OP_LABEL[o.kind]}</td><td>{o.track_id?.split("-T").pop()}</td><td className="nowrap">{fmtHM(o.start)}–{fmtHM(o.end)}</td><td>{o.resource_ids.join(", ") || "—"}</td></tr>))}</tbody></table></>
      )}
      {chk.alternatives?.length > 0 && (
        <><h3>Проверенные альтернативы</h3>
          {chk.alternatives.map((a: any, i: number) => {
            const need = { postpone: "request.reschedule", other_station: "request.cancel", split: "request.create", reorder: "operation.reschedule" }[a.type as string]!;
            return (
              <div key={i} className="callout">
                <div className="row between wrap"><b>{a.title}</b><Badge cls="ok" icon="✓">проверено</Badge></div>
                <div className="muted">{a.description}</div>
                <dl className="kv" style={{ marginTop: 6 }}>
                  <dt>Задержка</dt><dd>{fmtMin(a.effect.delay_min)}{a.effect.extra_travel_min ? ` · дополнительно в пути ${fmtMin(a.effect.extra_travel_min)}` : ""}</dd>
                  <dt>Загрузка</dt><dd>{a.effect.load_change}</dd>
                  <dt>Затронутые операции</dt><dd>{a.effect.affected_operations?.length ? a.effect.affected_operations.map((o: any) => `${OP_LABEL[o.kind]} № ${o.train}: ${o.from} → ${o.to}`).join("; ") : "нет"}</dd>
                </dl>
                <ActionButton small kind="primary" disabledReason={can(user, need) ? null : `Действие доступно ролям с правом «${need}».`}
                  onClick={async () => {
                    try {
                      await api.post(`/api/v1/requests/${request.id}/apply-alternative`, { action: a.action });
                      useStore.getState().toast("ok", `Применено: ${a.title}`, a.type === "reorder" ? "Теперь выполните проверку и подтверждение заявки." : undefined);
                      onApplied();
                    } catch (e) { notifyError(e); }
                  }}>Применить вариант</ActionButton>
              </div>);
          })}</>
      )}
      <details><summary>Допущения расчёта</summary><ul>{chk.assumptions.map((a: string, i: number) => <li key={i}>{a}</li>)}</ul></details>
    </div>
  );
}

function ConfirmDialog({ q, chk, onClose, onDone, onError }: { q: any; chk: any; onClose: () => void; onDone: () => void; onError: (e: ApiError) => void }) {
  const warns = chk.items.filter((i: any) => i.status === "warning");
  const [ack, setAck] = useState(false);
  const key = useState(newKey())[0];
  const [err, setErr] = useState<ApiError | null>(null);
  const submit = async () => {
    try {
      const r = await api.post(`/api/v1/requests/${q.id}/confirm`, { acknowledge_warnings: ack }, key);
      useStore.getState().toast("ok", `Заявка ${q.number} подтверждена: поезд № ${r.train_number}`, "Ресурсы зарезервированы, действие записано в аудит.");
      onDone();
    } catch (e) { setErr(e as ApiError); }
  };
  return (
    <Modal title={`Подтвердить заявку ${q.number}?`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Отмена</button>
        <button className="btn primary" disabled={warns.length > 0 && !ack} onClick={submit}>Подтвердить и зарезервировать</button></>}>
      <p style={{ margin: 0 }}>Сервер повторно проверит все ограничения на текущем состоянии и только затем создаст резервы.</p>
      {chk.assignment && <div className="callout"><b>Будет зарезервировано:</b><ul>{chk.assignment.ops.map((o: any) =>
        <li key={o.seq}>{OP_LABEL[o.kind]}: путь {o.track_id?.split("-T").pop()}, {fmtHM(o.start)}–{fmtHM(o.end)}{o.resource_ids.length ? `, ${o.resource_ids.join(", ")}` : ""}{o.route_nodes.length ? `, стрелки ${o.route_nodes.map((n: string) => n.split("-").pop()).join(", ")}` : ""}</li>)}</ul>
        <div className="muted">Другие операции не изменяются.</div></div>}
      {warns.length > 0 && <div className="callout warn"><b>Предупреждения:</b><ul>{warns.map((w: any) => <li key={w.code}>{w.message}</li>)}</ul>
        <label className="row"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> С предупреждениями ознакомлен</label></div>}
      {err && <ErrorBox error={err} />}
      {err?.details?.check && <div className="callout bad">Актуальная проверка: {err.details.check.summary}</div>}
    </Modal>
  );
}

function MoveDialog({ q, onClose, onDone }: { q: any; onClose: () => void; onDone: () => void }) {
  const [dep, setDep] = useState(toLocal(q.last_check?.nearest_window?.departure ?? q.desired_departure));
  const [err, setErr] = useState<ApiError | null>(null);
  const key = useState(newKey())[0];
  return (
    <Modal title={`Перенести отправление по заявке ${q.number}`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Отмена</button><button className="btn primary" onClick={async () => {
        try { await api.post(`/api/v1/requests/${q.id}/reschedule`, { departure: fromLocal(dep) }, key); useStore.getState().toast("ok", "Отправление перенесено, проверка выполнена"); onDone(); }
        catch (e) { setErr(e as ApiError); }
      }}>Перенести и проверить</button></>}>
      <p style={{ margin: 0 }}>{q.status === "confirmed" ? "Для подтверждённой заявки резервы будут заменены только если новое время проходит все проверки; иначе прежние резервы сохранятся." : "После переноса проверка приёма выполняется автоматически."}</p>
      {q.last_check?.nearest_window && <div className="callout ok">Ближайшее допустимое окно: отправление {fmtHM(q.last_check.nearest_window.departure)}, прибытие {fmtHM(q.last_check.nearest_window.arrival)}</div>}
      <label className="f">Новое время отправления (UTC+5)<input type="datetime-local" value={dep} onChange={(e) => setDep(e.target.value)} /></label>
      {err && <ErrorBox error={err} />}
    </Modal>
  );
}

function ReasonDialog({ q, kind, onClose, onDone }: { q: any; kind: "cancel" | "reject"; onClose: () => void; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const key = useState(newKey())[0];
  return (
    <Modal title={kind === "cancel" ? `Отменить заявку ${q.number}` : `Отказать по заявке ${q.number}`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Назад</button><button className="btn danger" disabled={reason.trim().length < 3} onClick={async () => {
        try { const r = await api.post(`/api/v1/requests/${q.id}/${kind}`, { reason }, key); useStore.getState().toast("ok", kind === "cancel" ? `Заявка отменена${r.released_reservations ? `, освобождено резервов: ${r.released_reservations}` : ""}` : "Отказ зарегистрирован"); onDone(); }
        catch (e) { setErr(e as ApiError); }
      }}>{kind === "cancel" ? "Отменить заявку" : "Отказать"}</button></>}>
      {kind === "cancel" && q.status === "confirmed" && <div className="callout warn">Будут освобождены все резервы по заявке (путь, стрелки, бригады).</div>}
      <label className="f">Основание (обязательно)<textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
      {err && <ErrorBox error={err} />}
    </Modal>
  );
}
