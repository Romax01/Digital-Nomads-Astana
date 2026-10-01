import { useState } from "react";
import { api } from "../lib/api";
import { ago, fmtFull, fmtHM, fmtMin, ms } from "../lib/format";
import { DATA_STATE, SEVERITY, TRACK_STATUS } from "../lib/labels";
import { can, useStore } from "../lib/store";
import type { ViewState } from "../lib/types";
import { DeviceCard, SectionCard, StationCard, ZoneCard } from "../twin/Cards";
import { IncidentDialog, OverrideDialog, RescheduleOpDialog } from "./Dialogs";
import { useNow } from "./Header";
import { ActionButton, Badge, Empty, useFetch } from "./ui";

function ConflictList({ st, ids }: { st: ViewState; ids: string[] }) {
  const select = useStore((s) => s.select);
  const list = ids.map((i) => st.conflicts[i]).filter(Boolean);
  if (!list.length) return null;
  return (
    <div className="col" style={{ gap: 6 }}>
      <h4>Конфликты ({list.length})</h4>
      {list.map((c) => (
        <div key={c.id} className="callout bad" role="button" tabIndex={0} style={{ cursor: "pointer" }}
          onClick={() => select({ type: "conflict", id: c.id })} onKeyDown={(e) => e.key === "Enter" && select({ type: "conflict", id: c.id })}>
          <b className={SEVERITY[c.severity].cls}>{SEVERITY[c.severity].icon} {c.title}</b>
          <div>{c.explanation}</div>
        </div>
      ))}
    </div>
  );
}

function OpsTable({ st, filter }: { st: ViewState; filter: (o: any) => boolean }) {
  const select = useStore((s) => s.select);
  const ops = Object.values(st.operations).filter(filter).sort((a, b) => ms(a.forecast_start) - ms(b.forecast_start)).slice(0, 12);
  if (!ops.length) return <p className="muted">Операций в окне планирования нет.</p>;
  return (
    <table className="t">
      <thead><tr><th>Операция</th><th>Время</th><th>Статус</th></tr></thead>
      <tbody>
        {ops.map((o) => (
          <tr key={o.id} className="clickable" onClick={() => select({ type: "operation", id: o.id })}>
            <td>{o.conflict_ids.length ? "⚠ " : ""}{o.kind_label}{o.train_number ? ` · № ${o.train_number}` : ""}</td>
            <td className="nowrap">{fmtHM(o.forecast_start)}–{fmtHM(o.forecast_end)}{o.delay_min >= 5 ? <span className="badge warn" style={{ marginLeft: 4 }}>+{o.delay_min}′</span> : null}</td>
            <td>{o.status_label}{!o.reserved ? " (не спланирована)" : ""}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function ObjectCard({ st }: { st: ViewState }) {
  const sel = useStore((s) => s.selection);
  const user = useStore((s) => s.user);
  const mode = useStore((s) => s.mode);
  const select = useStore((s) => s.select);
  const [dialog, setDialog] = useState<any>(null);
  const now = useNow();
  const readonly = mode === "history" ? "В режиме «История» изменения недоступны — вернитесь в «Сейчас»." : null;
  if (!sel) return <Empty text="Выберите объект" hint="Нажмите на путь, состав, стрелку или операцию на схеме, Ганте или в списке конфликтов." />;
  const incReason = readonly ?? (can(user, "incident.manage") ? null : "Регистрация инцидентов — станционный диспетчер или дежурный по станции.");

  if (sel.type === "track") {
    const t = st.tracks[sel.id];
    if (!t) return <Empty text="Путь не найден в текущем состоянии" />;
    const info = TRACK_STATUS[t.status];
    const ds = DATA_STATE[t.data_state] ?? DATA_STATE.missing;
    const age = t.data_observed_at ? (now - ms(t.data_observed_at)) / 1000 : null;
    return (
      <div className="col">
        <div className="row between"><h2 style={{ margin: 0 }}>{t.label}</h2><Badge cls={info.cls} icon={info.icon}>{info.label}</Badge></div>
        <dl className="kv">
          <dt>Назначение</dt><dd>{{ receiving_departure: "Приёмо-отправочный", sorting: "Сортировочный", cargo: "Грузовой фронт", repair: "Ремонтный", main: "Главный" }[t.kind] ?? t.kind}</dd>
          <dt>Полезная длина</dt><dd>{t.useful_length_m ? `${t.useful_length_m} м` : <b className="unknown">не задана — недостаточно данных</b>}</dd>
          <dt>Занят</dt><dd>{t.occupant_number ? <a href="#" onClick={(e) => { e.preventDefault(); select({ type: "train", id: t.occupant_train_id! }); }}>поезд № {t.occupant_number}</a> : "—"}</dd>
          <dt>Данные датчика</dt><dd><Badge cls={ds.cls} icon={ds.icon}>{ds.label}</Badge> {age !== null && <span className="muted">измерение {ago(age)}</span>}</dd>
          <dt>Показание</dt><dd>{t.observed === "occupied" ? "занят" : t.observed === "free" ? "свободен" : "нет"}</dd>
          {t.next_reservation && <><dt>Ближайший резерв</dt><dd>{fmtHM(t.next_reservation.start)}–{fmtHM(t.next_reservation.end)}: {t.next_reservation.purpose}</dd></>}
          {t.closure && <><dt>Закрытие</dt><dd className="bad">{t.closure.title} до {t.closure.until ? fmtHM(t.closure.until) : "отмены"}</dd></>}
        </dl>
        {t.data_state !== "actual" && t.data_message && <div className="callout unknown" role="alert">{t.data_message}</div>}
        <ConflictList st={st} ids={t.conflict_ids} />
        <h4>Операции на пути</h4>
        <OpsTable st={st} filter={(o) => (o.track_id === t.id || o.from_track_id === t.id) && o.status !== "done"} />
        <div className="row wrap">
          {t.status !== "closed" && <ActionButton small kind="danger" disabledReason={incReason} onClick={() => setDialog({ kind: "track_closure" })}>Закрыть путь</ActionButton>}
          {t.closure && <ActionButton small disabledReason={incReason} onClick={async () => {
            await api.post(`/api/v1/incidents/${t.closure!.incident_id}/resolve`, { reason: "Путь открыт после устранения" });
            useStore.getState().toast("ok", `${t.label} открыт`);
          }}>Открыть путь</ActionButton>}
          {["stale", "missing", "contradictory", "invalid"].includes(t.data_state) && (
            <ActionButton small disabledReason={readonly ?? (can(user, "observation.override") ? null : "Ручное уточнение — только дежурный по станции.")}
              onClick={() => setDialog({ kind: "override" })}>Уточнить показания</ActionButton>
          )}
        </div>
        {dialog?.kind === "override" && <OverrideDialog trackId={t.id} label={t.label} onClose={() => setDialog(null)} />}
        {dialog?.kind === "track_closure" && <IncidentDialog kind="track_closure" objectId={t.id} objectLabel={t.label} onClose={() => setDialog(null)} />}
      </div>
    );
  }
  if (sel.type === "train") return <TrainCard st={st} id={sel.id} incReason={incReason} />;
  if (sel.type === "operation") {
    const o = st.operations[sel.id];
    if (!o) return <Empty text="Операция вне окна отображения" />;
    const res = o.resource_ids.map((r) => st.resources[r]?.name ?? r);
    const moveReason = readonly ?? (!can(user, "operation.reschedule") ? "Ручной перенос — станционный диспетчер." :
      ["in_progress", "done", "cancelled"].includes(o.status) ? "Операция уже начата или завершена." : null);
    return (
      <div className="col">
        <div className="row between"><h2 style={{ margin: 0 }}>{o.kind_label}</h2><Badge cls={o.status === "in_progress" ? "info" : o.status === "done" ? "muted" : "ok"}>{o.status_label}</Badge></div>
        <dl className="kv">
          <dt>Поезд</dt><dd>{o.train_id ? <a href="#" onClick={(e) => { e.preventDefault(); select({ type: "train", id: o.train_id! }); }}>№ {o.train_number}</a> : (o.note ?? "—")}</dd>
          <dt>Путь</dt><dd>{st.tracks[o.track_id ?? ""]?.label ?? "—"}{o.from_track_id ? ` (с: ${st.tracks[o.from_track_id]?.label})` : ""}</dd>
          <dt>План</dt><dd>{fmtHM(o.planned_start)}–{fmtHM(o.planned_end)} ({fmtMin(o.duration_min)})</dd>
          <dt>Прогноз</dt><dd>{fmtHM(o.forecast_start)}–{fmtHM(o.forecast_end)}{o.delay_min >= 1 ? <span className="badge warn" style={{ marginLeft: 4 }}>задержка {fmtMin(o.delay_min)}</span> : null}</dd>
          {o.actual_start && <><dt>Факт</dt><dd>{fmtHM(o.actual_start)}–{o.actual_end ? fmtHM(o.actual_end) : "…"}</dd></>}
          <dt>Ресурсы</dt><dd>{res.length ? res.join(", ") : "не требуются"}</dd>
          {o.route_nodes.length > 0 && <><dt>Маршрут</dt><dd className="mono">стрелки {o.route_nodes.map((n) => n.split("-").pop()).join(" → ")}</dd></>}
          {o.extra_delay_min > 0 && <><dt>Задержка от инцидента</dt><dd>+{o.extra_delay_min} мин</dd></>}
        </dl>
        {!o.reserved && <div className="callout warn">Операция ещё не спланирована (нет резерва): зависящие операции не начнутся до её размещения планировщиком.</div>}
        <ConflictList st={st} ids={o.conflict_ids} />
        <ActionButton small disabledReason={moveReason} onClick={() => setDialog({ kind: "move" })}>Перенести операцию</ActionButton>
        {dialog?.kind === "move" && <RescheduleOpDialog op={o} tracks={Object.values(st.tracks)} onClose={() => setDialog(null)} />}
      </div>
    );
  }
  if (sel.type === "resource") {
    const r = st.resources[sel.id];
    if (!r) return <Empty text="Ресурс не найден" />;
    return (
      <div className="col">
        <div className="row between"><h2 style={{ margin: 0 }}>{r.name}</h2><Badge cls={r.status === "faulty" ? "bad" : r.status === "busy" ? "info" : r.status === "off_shift" ? "muted" : "ok"}>{r.status_label}</Badge></div>
        <dl className="kv"><dt>Вид</dt><dd>{r.kind_label}</dd><dt>Зона</dt><dd>{r.zone ?? "—"}</dd></dl>
        <ConflictList st={st} ids={r.conflict_ids} />
        <h4>Операции ресурса</h4>
        <OpsTable st={st} filter={(o) => o.resource_ids.includes(r.id) && o.status !== "done"} />
        {r.status !== "faulty" && <ActionButton small kind="danger" disabledReason={incReason} onClick={() => setDialog({ kind: "resource_failure" })}>Зарегистрировать отказ</ActionButton>}
        {dialog && <IncidentDialog kind="resource_failure" objectId={r.id} objectLabel={r.name} onClose={() => setDialog(null)} />}
      </div>
    );
  }
  if (sel.type === "switch") {
    const s = st.switches[sel.id];
    return (
      <div className="col">
        <h2>{s?.name ?? sel.id}</h2>
        <dl className="kv">
          <dt>Наблюдаемое положение</dt><dd>{s?.position ? (s.position === "normal" ? "плюсовое" : s.position === "reverse" ? "минусовое" : "неизвестно") : "нет данных"}{s?.observed_at ? ` (${fmtHM(s.observed_at)})` : ""}</dd>
          <dt>В маршруте</dt><dd>{s?.route_op ? `${st.operations[s.route_op]?.kind_label ?? "движение"} № ${st.operations[s.route_op]?.train_number ?? ""}` : "нет"}</dd>
          <dt>Состояние</dt><dd>{s?.closed ? <b className="bad">неисправна</b> : "исправна"}</dd>
        </dl>
        <p className="faint">Положение стрелки только наблюдается: система не управляет стрелками.</p>
        <ConflictList st={st} ids={s?.conflict_ids ?? []} />
        <ActionButton small kind="danger" disabledReason={incReason} onClick={() => setDialog({ kind: "switch_failure" })}>Неисправность стрелки</ActionButton>
        {dialog && <IncidentDialog kind="switch_failure" objectId={sel.id} objectLabel={s?.name ?? sel.id} onClose={() => setDialog(null)} />}
      </div>
    );
  }
  if (sel.type === "conflict") {
    const c = st.conflicts[sel.id];
    if (!c) return <Empty text="Конфликт устранён" hint="Состояние изменилось — конфликт больше не обнаруживается." />;
    return (
      <div className="col">
        <h2 className={SEVERITY[c.severity].cls}>{SEVERITY[c.severity].icon} {c.title}</h2>
        <Badge cls={c.severity === "critical" ? "bad" : "warn"}>{c.severity_label}</Badge>
        <p>{c.explanation}</p>
        <dl className="kv"><dt>Интервал</dt><dd>{fmtHM(c.start)}–{c.end ? fmtHM(c.end) : "…"}</dd><dt>Обнаружен</dt><dd>{fmtFull(c.detected_at)} (модельное)</dd></dl>
        <h4>Затронутые объекты</h4>
        <div className="row wrap">
          {c.objects.map((o) => (
            <button key={o.type + o.id} className="btn small" onClick={() => ["track", "train", "resource", "switch"].includes(o.type) && select({ type: o.type as any, id: o.id })}>{o.label}</button>
          ))}
          {c.operations.filter((oid) => st.operations[oid]).slice(0, 6).map((oid) => (
            <button key={oid} className="btn small" onClick={() => select({ type: "operation", id: oid })}>{st.operations[oid].kind_label} № {st.operations[oid].train_number}</button>
          ))}
        </div>
      </div>
    );
  }
  if (sel.type === "zone") return <ZoneCard st={st} id={sel.id} />;
  if (sel.type === "station") return <StationCard st={st} id={sel.id} />;
  if (sel.type === "section") return <SectionCard st={st} id={sel.id} />;
  if (sel.type === "device") return <DeviceCard id={sel.id} />;
  return <Empty text="Нет карточки для объекта" />;
}

const WAGON_RU: Record<string, string> = { gondola: "полувагон", covered: "крытый", tank: "цистерна", flat: "платформа", hopper: "хоппер", passenger: "пассажирский" };

/** Сводка состава по типам: «полувагон 14 (гружёных 10) · цистерна 9». */
function consistSummary(c: NonNullable<ViewState["trains"][string]["consist"]>) {
  const by: Record<string, { n: number; loaded: number; faulty: number }> = {};
  for (const g of c.groups) {
    const x = (by[g.kind] ||= { n: 0, loaded: 0, faulty: 0 });
    x.n += g.count; if (g.loaded) x.loaded += g.count; if (g.faulty) x.faulty += g.count;
  }
  return Object.entries(by).map(([k, x]) => `${WAGON_RU[k] ?? k} ${x.n}${x.loaded && k !== "passenger" ? ` (гружёных ${x.loaded})` : ""}${x.faulty ? ` ⚠ ${x.faulty}` : ""}`).join(" · ");
}

function TrainCard({ st, id, incReason }: { st: ViewState; id: string; incReason: string | null }) {
  const t = st.trains[id];
  const [dlg, setDlg] = useState(false);
  const { data } = useFetch(() => api.get(`/api/v1/trains/${id}`), [id, t?.status]);
  if (!t) return <Empty text="Поезд вне окна отображения" hint="Он уже отправлен или прибудет позже." />;
  const wagon = data?.wagons?.find((w: any) => w.condition === "ok");
  const net = st.network?.trains[id];
  return (
    <div className="col">
      <div className="row between"><h2 style={{ margin: 0 }}>Поезд № {t.number}</h2><Badge cls={t.delay_min >= 5 ? "warn" : "ok"}>{t.status_label}</Badge></div>
      <dl className="kv">
        <dt>Вид / приоритет</dt><dd>{{ freight: "грузовой", transfer: "передаточный (заявка)", passenger: "пассажирский" }[t.kind] ?? t.kind} · {t.priority} из 5</dd>
        <dt>Состав</dt><dd>{t.wagons} ваг., {t.length_m ? `${t.length_m.toFixed(0)} м` : "длина неизвестна"}</dd>
        {t.consist && t.consist.groups.length > 0 && <><dt>Вагоны</dt><dd>{consistSummary(t.consist)}{t.consist.source !== "wagons" ? <span className="faint"> — тип по роду поезда</span> : null}</dd></>}
        {net && <><dt>Местоположение</dt><dd>{net.phase_label}{net.section_id ? ` · путь № ${net.track_no} перегона` : ""}{net.phase === "on_section" ? ` · пройдено ${Math.round((net.frac ?? 0) * 100)} %` : ""}</dd></>}
        <dt>Путь</dt><dd>{st.tracks[t.track_id ?? ""]?.label ?? "—"}</dd>
        <dt>Прибытие</dt><dd>{fmtHM(t.scheduled_arrival)} (прогноз {fmtHM(t.expected_arrival)})</dd>
        <dt>Отправление</dt><dd>{t.scheduled_departure ? `${fmtHM(t.scheduled_departure)} (прогноз ${fmtHM(t.expected_departure)})` : "переработка на станции"}</dd>
        <dt>Задержка</dt><dd>{t.delay_min ? <b className="warn">{fmtMin(t.delay_min)}</b> : "нет"}</dd>
        {t.waiting_reason && <><dt>Ожидает</dt><dd className="warn">{t.waiting_reason}</dd></>}
        {t.faulty_wagons.length > 0 && <><dt>Неисправные вагоны</dt><dd className="bad">⚠ № {t.faulty_wagons.join(", ")}</dd></>}
      </dl>
      <ConflictList st={st} ids={t.conflict_ids} />
      <h4>Операции поезда</h4>
      <OpsTable st={st} filter={(o) => o.train_id === t.id} />
      {wagon && t.status === "on_station" && (
        <ActionButton small kind="danger" disabledReason={incReason} onClick={() => setDlg(true)}>Сообщить о неисправном вагоне</ActionButton>
      )}
      {dlg && wagon && <IncidentDialog kind="faulty_wagon" objectId={wagon.id} objectLabel={`вагон № ${wagon.number} (поезд № ${t.number})`} onClose={() => setDlg(false)} />}
    </div>
  );
}
