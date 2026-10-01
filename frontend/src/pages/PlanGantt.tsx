import { useState } from "react";
import Gantt from "../components/Gantt";
import ObjectCard from "../components/ObjectCard";
import { ActionButton, Badge, Empty, ErrorBox, Loading, notifyError, useFetch } from "../components/ui";
import { api } from "../lib/api";
import { fmtHM, fmtHMS, fmtMin } from "../lib/format";
import { can, useStore, useViewState } from "../lib/store";

const STATUS_CLS: Record<string, string> = { optimal: "ok", feasible: "info", heuristic: "info", timeout: "warn", infeasible: "bad", partial: "warn" };

export default function PlanGantt() {
  const st = useViewState();
  const user = useStore((s) => s.user);
  const mode = useStore((s) => s.mode);
  const [by, setBy] = useState<"tracks" | "resources">("tracks");
  const [hours, setHours] = useState(8);
  const [showProposed, setShowProposed] = useState(false);
  const [busy, setBusy] = useState(false);
  const planId = st?.plan?.id;
  const plan = useFetch(() => (planId ? api.get(`/api/v1/plans/${planId}`) : Promise.resolve(null)), [planId, st?.plan?.status, st?.meta.state_version]);
  if (!st) return <Loading />;
  const p = plan.data;
  const override = showProposed && p ? Object.fromEntries(p.assignments.map((a: any) => [a.operation_id, a])) : undefined;
  const compute = async () => {
    setBusy(true);
    try { const r = await api.postPlain("/api/v1/plans/compute"); useStore.getState().toast("ok", `План № ${r.id}: ${r.solver_status_label}`, `${Math.round(r.solve_ms)} мс`); }
    catch (e) { notifyError(e); } finally { setBusy(false); }
  };
  const ro = mode === "history" ? "Недоступно в режиме «История»." : null;
  const applyReason = ro ?? (!p ? "Нет предложенного плана." : p.status === "stale" ? `План устарел: рассчитан для версии ${p.base_state_version}, текущая — ${p.current_state_version}. Пересчитайте.` :
    p.status !== "proposed" ? `План в статусе «${p.status}».` : !p.changes.length ? "План не содержит изменений." :
      !can(user, "plan.apply") ? "Применение плана — станционный диспетчер." : null);
  const sb = p?.summary?.before, sa = p?.summary?.after;
  return (
    <div className="page" style={{ maxWidth: "none" }}>
      <div className="row between wrap">
        <h1>План и диаграмма Ганта</h1>
        <div className="row wrap">
          <div className="seg" role="group" aria-label="Строки">
            <button aria-pressed={by === "tracks"} onClick={() => setBy("tracks")}>По путям</button>
            <button aria-pressed={by === "resources"} onClick={() => setBy("resources")}>По локомотивам и бригадам</button>
          </div>
          <label className="row">Горизонт <select value={hours} onChange={(e) => setHours(+e.target.value)}>{[4, 8, 12].map((h) => <option key={h} value={h}>{h} ч</option>)}</select></label>
          <label className="row"><input type="checkbox" checked={showProposed} onChange={(e) => setShowProposed(e.target.checked)} disabled={!p} /> Показать предложенный план</label>
        </div>
      </div>
      {showProposed && <div className="mode-banner forecast">На диаграмме — предложенный план № {p?.id} (ещё не применён). Снимите отметку, чтобы вернуться к текущему плану.</div>}
      <div className="grid2" style={{ gridTemplateColumns: "minmax(0, 3fr) minmax(320px, 1fr)" }}>
        <section className="panel panel-b"><Gantt st={st} by={by} hoursBefore={1} hoursAfter={hours} override={override} /></section>
        <aside className="panel panel-b"><ObjectCard st={st} /></aside>
      </div>
      <section className="panel">
        <div className="panel-h wrap">
          <h2 className="grow">Сравнение планов</h2>
          <ActionButton busy={busy} disabledReason={ro ?? (can(user, "plan.compute") ? null : "Расчёт доступен диспетчерам и дежурному.")} onClick={compute}>Пересчитать план</ActionButton>
          <ActionButton kind="primary" disabledReason={applyReason} onClick={async () => {
            try { const r = await api.post(`/api/v1/plans/${p.id}/apply`); useStore.getState().toast("ok", `План № ${p.id} применён: ${r.changed_operations} операций`, `Поезда: ${r.trains.join(", ")}`); plan.reload(); }
            catch (e) { notifyError(e); }
          }}>Применить план</ActionButton>
        </div>
        <div className="panel-b col">
          {plan.loading && !p ? <Loading /> : plan.error ? <ErrorBox error={plan.error} retry={plan.reload} /> : !p ? (
            <Empty text="Предложенного плана нет" hint="Планировщик запускается автоматически при конфликтах; можно рассчитать вручную." />
          ) : (<>
            <div className="row wrap">
              <Badge cls={STATUS_CLS[p.solver_status] ?? "muted"}>{p.solver_status_label}</Badge>
              <Badge cls={p.status === "proposed" ? "info" : p.status === "applied" ? "ok" : "warn"}>{{ proposed: "Предложен", applied: "Применён", stale: "Устарел", rejected: "Отклонён" }[p.status as string] ?? p.status}</Badge>
              <span className="muted">План № {p.id} · {p.solver} · расчёт {Math.round(p.solve_ms)} мс (всего {Math.round(p.summary.total_ms ?? p.solve_ms)} мс) · модельное время {fmtHMS(p.model_time)} · версия состояния {p.base_state_version} · повод: {p.trigger}</span>
            </div>
            <p className="muted" style={{ margin: 0 }}><b>Цель:</b> {p.summary.objective_text}</p>
            <table className="t" style={{ maxWidth: 760 }}>
              <thead><tr><th>Показатель</th><th>Текущий план (прогноз)</th><th>Предложенный план</th></tr></thead>
              <tbody>
                <tr><td>Конфликты</td><td>{p.summary.conflicts_before}</td><td><b>{sa.conflicts}</b></td></tr>
                <tr><td>Суммарная задержка отправлений</td><td>{fmtMin(sb.total_delay_min)}</td><td><b>{fmtMin(sa.total_delay_min)}</b></td></tr>
                <tr><td>Взвешенная задержка (по приоритетам)</td><td>{sb.weighted_delay}</td><td><b>{sa.weighted_delay}</b></td></tr>
                <tr><td>Поездов с задержкой ≥ 5 мин</td><td>{sb.delayed_trains}</td><td><b>{sa.delayed_trains}</b></td></tr>
                <tr><td>Максимальная задержка</td><td>{fmtMin(sb.max_delay_min)}</td><td><b>{fmtMin(sa.max_delay_min)}</b></td></tr>
                <tr><td>Загрузка парка А (4 ч)</td><td>{Math.round(sb.rd_utilization_4h * 100)}%</td><td><b>{Math.round(sa.rd_utilization_4h * 100)}%</b></td></tr>
                <tr><td>Индекс эффективности (прогноз)</td><td>{p.summary.index_before?.value ?? "—"}</td><td><b>{p.summary.index_after?.value ?? "—"}</b></td></tr>
              </tbody>
            </table>
            {p.summary.unresolved?.length > 0 && <div className="callout warn"><b>Не решено планировщиком:</b><ul>{p.summary.unresolved.map((u: any, i: number) => <li key={i}>{u.train ? `Поезд № ${u.train}: ` : ""}{u.reason}</li>)}</ul></div>}
            {p.summary.notes?.length > 0 && <div className="callout"><ul>{p.summary.notes.map((n: string, i: number) => <li key={i}>{n}</li>)}</ul></div>}
            <h3>Изменённые операции ({p.changes.length})</h3>
            {p.changes.length ? (
              <div style={{ maxHeight: 360, overflow: "auto" }}>
                <table className="t">
                  <thead><tr><th>Поезд</th><th>Операция</th><th>Было</th><th>Стало</th><th>Сдвиг</th><th>Примечание</th></tr></thead>
                  <tbody>{p.changes.map((c: any) => (
                    <tr key={c.operation_id} className="clickable" onClick={() => useStore.getState().select({ type: "operation", id: c.operation_id })}>
                      <td>{c.train ? `№ ${c.train}` : "—"}</td><td>{c.kind_label}</td>
                      <td>{c.from.track} · {fmtHM(c.from.start)}{c.from.resources.length ? ` · ${c.from.resources.join(", ")}` : ""}</td>
                      <td><b>{c.to.track}</b> · {fmtHM(c.to.start)}{c.to.resources.length ? ` · ${c.to.resources.join(", ")}` : ""}</td>
                      <td>{c.shift_min ? `${c.shift_min > 0 ? "+" : ""}${c.shift_min} мин` : "—"}</td>
                      <td>{[c.track_changed && "смена пути", c.resources_changed && "смена ресурса", c.newly_planned && "впервые спланирована", c.was_confirmed && "была согласована"].filter(Boolean).join(", ")}</td>
                    </tr>))}</tbody>
                </table>
              </div>) : <p className="muted">Изменений нет — текущий план допустим.</p>}
            <details><summary>Допущения модели</summary><ul>{p.assumptions.map((a: string, i: number) => <li key={i}>{a}</li>)}</ul></details>
          </>)}
        </div>
      </section>
    </div>
  );
}
