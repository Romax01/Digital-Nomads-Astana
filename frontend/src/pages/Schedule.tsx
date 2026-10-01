import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Badge, Empty, ErrorBox, Loading, useFetch } from "../components/ui";
import { api } from "../lib/api";
import { fmtHM } from "../lib/format";
import { useStore, useViewState } from "../lib/store";

export default function Schedule() {
  const st = useViewState();
  const select = useStore((s) => s.select);
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [onlyDelayed, setOnlyDelayed] = useState(false);
  const d = useFetch(() => api.get("/api/v1/schedule"), [Math.floor((st ? new Date(st.meta.model_time).getTime() : 0) / 60000)]);
  if (d.loading && !d.data) return <Loading />;
  if (d.error) return <ErrorBox error={d.error} retry={d.reload} />;
  const rows = d.data.rows.filter((r: any) => (!q || r.number.includes(q)) && (!onlyDelayed || r.delay_min >= 5));
  return (
    <div className="page">
      <div className="row between wrap"><h1>Расписание</h1>
        <div className="row"><input placeholder="Номер поезда" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Поиск по номеру" />
          <label className="row"><input type="checkbox" checked={onlyDelayed} onChange={(e) => setOnlyDelayed(e.target.checked)} /> Только с задержкой</label></div></div>
      <p className="muted" style={{ margin: 0 }}>Прогноз рассчитан на {fmtHM(d.data.model_time)} модельного времени по технологической цепочке операций. Время — UTC+5.</p>
      <section className="panel">
        {!rows.length ? <Empty text="Нет поездов по фильтру" /> : (
          <table className="t">
            <thead><tr><th>Поезд</th><th>Вид</th><th>Откуда → куда</th><th>Ваг.</th><th>Путь</th><th>Прибытие: план / прогноз</th><th>Отправление: план / прогноз</th><th>Задержка</th><th>Статус</th></tr></thead>
            <tbody>{rows.map((r: any) => (
              <tr key={r.id} className="clickable" tabIndex={0} onClick={() => { select({ type: "train", id: r.id }); nav("/"); }}
                onKeyDown={(e) => { if (e.key === "Enter") { select({ type: "train", id: r.id }); nav("/"); } }}>
                <td><b>№ {r.number}</b>{r.transfer_request_id ? <div className="faint">по заявке</div> : null}</td>
                <td>{{ freight: "грузовой", transfer: "передаточный", passenger: "пассажирский" }[r.kind as string] ?? r.kind} · п{r.priority}</td>
                <td>{r.origin ?? "—"} → {r.destination ?? "—"}</td><td>{r.wagons}</td><td>{r.track_label ?? "—"}</td>
                <td className="nowrap">{fmtHM(r.scheduled_arrival)} / {fmtHM(r.forecast_arrival)}</td>
                <td className="nowrap">{r.scheduled_departure ? `${fmtHM(r.scheduled_departure)} / ${fmtHM(r.forecast_departure)}` : "переработка"}</td>
                <td>{r.delay_min >= 5 ? <Badge cls={r.delay_min >= 30 ? "bad" : "warn"}>+{r.delay_min} мин</Badge> : <span className="muted">—</span>}</td>
                <td>{r.status_label}</td>
              </tr>))}</tbody>
          </table>)}
      </section>
    </div>
  );
}
