import { useState } from "react";
import { Badge, Empty, ErrorBox, Loading, useFetch } from "../components/ui";
import { api } from "../lib/api";
import { fmtHMS } from "../lib/format";

export default function Journal() {
  const [tab, setTab] = useState<"events" | "audit">("events");
  const ev = useFetch(() => api.get("/api/v1/events?limit=300"), [tab], tab === "events" ? 3000 : undefined);
  const au = useFetch(() => api.get("/api/v1/audit?limit=300"), [tab], tab === "audit" ? 5000 : undefined);
  return (
    <div className="page">
      <h1>Журнал</h1>
      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "events"} onClick={() => setTab("events")}>События станции</button>
        <button role="tab" aria-selected={tab === "audit"} onClick={() => setTab("audit")}>Аудит изменений</button>
      </div>
      <section className="panel">
        {tab === "events" && (ev.loading && !ev.data ? <Loading /> : ev.error ? <ErrorBox error={ev.error} /> : !ev.data?.length ? <Empty text="Событий нет" /> : (
          <table className="t"><thead><tr><th>Реальное</th><th>Модельное</th><th>Тип</th><th>Сообщение</th></tr></thead>
            <tbody>{ev.data.map((e: any) => (
              <tr key={e.id}><td className="nowrap">{fmtHMS(e.ts)}</td><td className="nowrap">{fmtHMS(e.model_time)}</td>
                <td><Badge cls={e.severity === "warning" ? "warn" : "muted"}>{e.type}</Badge></td><td>{e.message}</td></tr>))}</tbody></table>))}
        {tab === "audit" && (au.loading && !au.data ? <Loading /> : au.error ? <ErrorBox error={au.error} /> : !au.data?.length ? <Empty text="Записей аудита нет" /> : (
          <table className="t"><thead><tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Описание</th><th>Основание</th></tr></thead>
            <tbody>{au.data.map((a: any) => (
              <tr key={a.id}><td className="nowrap">{fmtHMS(a.ts)}<div className="faint">модельное {fmtHMS(a.model_time)}</div></td>
                <td>{a.username}<div className="faint">{a.role_label}</div></td><td className="mono">{a.action}</td>
                <td>{a.summary}{(a.before || a.after) && <details><summary>Было / стало</summary><pre className="mono" style={{ whiteSpace: "pre-wrap", maxHeight: 200, overflow: "auto" }}>{JSON.stringify({ было: a.before, стало: a.after }, null, 1).slice(0, 3000)}</pre></details>}</td>
                <td>{a.reason ?? "—"}</td></tr>))}</tbody></table>))}
      </section>
    </div>
  );
}
