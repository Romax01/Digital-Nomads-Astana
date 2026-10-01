import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Badge, Empty, ErrorBox, Loading, Modal, useFetch } from "../components/ui";
import { api } from "../lib/api";
import { fmtHMS } from "../lib/format";
import { CONN, DATA_STATE, DEVICE_KIND } from "../lib/labels";
import { useStore } from "../lib/store";

export default function Devices() {
  const d = useFetch(() => api.get("/api/v1/devices"), [], 2000);
  const rej = useFetch(() => api.get("/api/v1/telemetry/rejects?limit=50"), [], 5000);
  const [kind, setKind] = useState("");
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const select = useStore((s) => s.select);
  const nav = useNavigate();
  if (d.loading && !d.data) return <Loading />;
  if (d.error) return <ErrorBox error={d.error} retry={d.reload} />;
  const list = d.data.devices.filter((x: any) => (!kind || x.kind === kind) && (!onlyProblems || x.connection !== "online" || x.data_quality !== "actual"));
  const ing = d.data.ingest;
  return (
    <div className="page">
      <div className="row between wrap"><h1>Датчики и связь</h1><Badge cls="warn">Симуляция: все устройства программные</Badge></div>
      <div className="row wrap">
        <Badge cls={ing.connected ? "ok" : "bad"} icon={ing.connected ? "●" : "✕"}>MQTT-брокер {ing.broker}: {ing.connected ? "подключено" : "нет связи"}</Badge>
        <span className="muted">Очередь приёма: значимые {ing.queue.significant}, координаты {ing.queue.coordinates}, отброшено при перегрузке {ing.queue.dropped} · переподключений {ing.disconnects}</span>
      </div>
      <p className="muted" style={{ margin: 0 }}>Состояние подключения (heartbeat) и качество последнего измерения — отдельные признаки. Устаревшие данные никогда не считаются «путь свободен».</p>
      <div className="row wrap">
        <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Тип устройства"><option value="">Все типы</option>{Object.entries(DEVICE_KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <label className="row"><input type="checkbox" checked={onlyProblems} onChange={(e) => setOnlyProblems(e.target.checked)} /> Только с проблемами</label>
      </div>
      <section className="panel" style={{ overflow: "auto" }}>
        {!list.length ? <Empty text="Нет устройств по фильтру" /> : (
          <table className="t">
            <thead><tr><th>Устройство</th><th>Тип</th><th>Объект</th><th>Связь</th><th>Качество данных</th><th>Возраст измерения</th><th>Порог</th><th>Источник</th></tr></thead>
            <tbody>{list.map((x: any) => {
              const c = CONN[x.connection] ?? CONN.unknown, q = DATA_STATE[x.data_quality] ?? DATA_STATE.missing;
              return (
                <tr key={x.id} className="clickable" tabIndex={0} onClick={() => setOpen(x.id)} onKeyDown={(e) => e.key === "Enter" && setOpen(x.id)}>
                  <td><b>{x.name}</b><div className="faint mono">{x.id}</div></td><td>{DEVICE_KIND[x.kind]}</td>
                  <td><a href="#" onClick={(e) => { e.preventDefault(); e.stopPropagation(); if (x.object_id.includes("-T")) { select({ type: "track", id: x.object_id }); nav("/"); } }}>{x.object_id}</a></td>
                  <td><Badge cls={c.cls} icon={c.icon}>{c.label}</Badge>{x.heartbeat_age_s !== null && <div className="faint">heartbeat {Math.round(x.heartbeat_age_s)} с назад</div>}</td>
                  <td><Badge cls={q.cls} icon={q.icon}>{q.label}</Badge>{x.health?.last_reject_reason && x.data_quality === "invalid" && <div className="faint">{x.health.last_reject_reason}</div>}</td>
                  <td>{x.measurement_age_s !== null ? `${Math.round(x.measurement_age_s)} с` : "—"}</td><td>{x.stale_after_s} с</td>
                  <td><Badge cls="warn">{x.source_mode === "simulator" ? "Симуляция" : "Реальное"}</Badge></td>
                </tr>);
            })}</tbody>
          </table>)}
      </section>
      <section className="panel">
        <div className="panel-h"><h2>Отклонённые сообщения (не применены к состоянию)</h2></div>
        {!rej.data?.length ? <Empty text="Отклонённых сообщений нет" /> : (
          <table className="t"><thead><tr><th>Приём</th><th>Устройство</th><th>Причина</th><th>Сообщение</th></tr></thead>
            <tbody>{rej.data.map((r: any, i: number) => <tr key={i}><td className="nowrap">{fmtHMS(r.received_at)}</td><td className="mono">{r.device_id ?? "—"}</td><td><b>{r.code}</b><div>{r.reason}</div></td><td className="mono faint" style={{ maxWidth: 380, wordBreak: "break-all" }}>{r.raw.slice(0, 160)}</td></tr>)}</tbody></table>)}
      </section>
      {open && <DeviceHistory id={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

function DeviceHistory({ id, onClose }: { id: string; onClose: () => void }) {
  const d = useFetch(() => api.get(`/api/v1/devices/${id}?limit=60`), [id], 3000);
  return (
    <Modal title={`История устройства ${id}`} onClose={onClose}>
      {!d.data ? <Loading /> : <>
        <dl className="kv">
          <dt>Сеанс (boot_id)</dt><dd className="mono">{d.data.device.last_boot_id ?? "—"} · последний № {d.data.device.last_seq ?? "—"}</dd>
          <dt>Период / порог устаревания</dt><dd>{d.data.device.period_s} с / {d.data.device.stale_after_s} с</dd>
          <dt>Разрешённые сообщения</dt><dd>{d.data.device.allowed_event_types.join(", ")}</dd>
          <dt>Техсостояние</dt><dd>{d.data.device.health?.battery ? `батарея ${d.data.device.health.battery}%, сигнал ${d.data.device.health.rssi} дБм` : "—"}</dd>
        </dl>
        <table className="t"><thead><tr><th>Измерено</th><th>Принято</th><th>Задержка</th><th>№</th><th>Значение</th><th>Обработка</th></tr></thead>
          <tbody>{d.data.events.map((e: any) => (
            <tr key={e.event_id}><td className="nowrap">{fmtHMS(e.observed_at)}</td><td className="nowrap">{fmtHMS(e.received_at)}</td><td>{e.latency_ms} мс</td><td>{e.seq}</td>
              <td className="mono">{JSON.stringify(e.payload).slice(0, 80)}</td>
              <td><Badge cls={e.disposition === "applied" ? "ok" : "warn"}>{{ applied: "применено", late: "опоздало (в истории)", stale: "устарело (в истории)" }[e.disposition as string] ?? e.disposition}</Badge></td></tr>))}</tbody></table>
        {d.data.rejects.length > 0 && <><h3>Ошибки</h3><ul>{d.data.rejects.map((r: any, i: number) => <li key={i}>{fmtHMS(r.received_at)} — {r.code}: {r.reason}</li>)}</ul></>}
      </>}
    </Modal>
  );
}
