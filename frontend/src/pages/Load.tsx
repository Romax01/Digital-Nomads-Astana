import { Badge, ErrorBox, Loading, useFetch } from "../components/ui";
import { api, download } from "../lib/api";
import { fmtHM, fmtHMS } from "../lib/format";
import { useStore, useViewState } from "../lib/store";

const CAT_CLS: Record<string, string> = { normal: "ok", attention: "warn", critical: "bad", unknown: "unknown" };

export default function Load() {
  const st = useViewState();
  const d = useFetch(() => api.get("/api/v1/capacity"), [st?.meta.state_version]);
  const idx = st?.index;
  return (
    <div className="page">
      <div className="row between wrap"><h1>Загрузка, ограничения и индекс</h1>
        <div className="row">
          <button className="btn" onClick={() => download("/api/v1/reports/mini?format=pdf&minutes=60", "мини-отчёт.pdf").catch((e) => useStore.getState().toast("error", e.message))}>⬇ Отчёт PDF</button>
          <button className="btn" onClick={() => download("/api/v1/reports/mini?format=csv&minutes=60", "мини-отчёт.csv").catch((e) => useStore.getState().toast("error", e.message))}>⬇ Отчёт CSV</button>
        </div></div>
      {idx && (
        <section className="panel">
          <div className="panel-h wrap"><h2 className="grow">Индекс эффективности станции</h2>
            <span className="muted">Формула: {idx.formula} · конфигурация v{idx.config_version} · расчёт {fmtHMS(idx.computed_at)} (модельное)</span></div>
          <div className="panel-b col">
            <div className="row wrap">
              <span className="index-num">{idx.value ?? "—"}</span><Badge cls={CAT_CLS[idx.category]}>{idx.category_label}</Badge>
              <span className="muted">Пороги: «Норма» ≥ {idx.thresholds.normal}, «Внимание» ≥ {idx.thresholds.attention}. Качество оценки: <b>{idx.quality.label}</b> ({Math.round(idx.quality.coverage * 100)}% весов с данными)</span>
            </div>
            <div className="callout">Индекс — аналитический показатель и не является разрешением на движение: жёсткие ограничения проверяются независимо от его значения.</div>
            <table className="t">
              <thead><tr><th>Составляющая</th><th>Вес</th><th>Значение</th><th>Оценка sᵢ</th><th>Пояснение</th><th>Источник</th></tr></thead>
              <tbody>{Object.values(idx.components).map((c) => (
                <tr key={c.key}><td><b>{c.title}</b>{c.window_min ? <div className="faint">окно {c.window_min} мин</div> : null}</td>
                  <td>{idx.weights[c.key]}</td><td>{c.raw ?? "—"} {c.raw !== null ? c.unit : ""}</td>
                  <td>{c.score === null ? <Badge cls="unknown" icon="?">нет данных</Badge> : <div className="row"><div className="bar" style={{ width: 80 }}><i style={{ width: `${c.score * 100}%` }} /></div>{c.score.toFixed(2)}</div>}</td>
                  <td>{c.explanation}</td><td className="faint">{c.source}</td></tr>))}</tbody>
            </table>
            {idx.factors.length > 0 && <div><b>Основные факторы снижения:</b> {idx.factors.map((f) => `${f.title.toLowerCase()} (−${f.loss_points})`).join("; ")}</div>}
          </div>
        </section>)}
      {d.loading && !d.data ? <Loading /> : d.error ? <ErrorBox error={d.error} retry={d.reload} /> : d.data && <>
        <section className="panel">
          <div className="panel-h"><h2 className="grow">Месячный план обработки вагонов ({d.data.plan.month})</h2><Badge cls="info">{d.data.plan.policy_label}</Badge></div>
          <div className="panel-b col">
            <div className="row wrap">
              <span>Цель: <b>{d.data.plan.target}</b> ваг.</span><span>Факт: <b>{d.data.plan.fact}</b></span><span>Согласовано (будущие операции): <b>{d.data.plan.agreed}</b></span>
              <span>Прогноз месяца: <b>{d.data.plan.fact + d.data.plan.agreed}</b></span>
            </div>
            <div className="bar"><i style={{ width: `${Math.min(100, (d.data.plan.fact / d.data.plan.target) * 100)}%` }} /></div>
            <p className="faint" style={{ margin: 0 }}>Месячный план — целевой показатель. Запрет новых заявок при превышении — отдельная политика «жёсткая квота» (раздел «Настройки»). Физическая занятость проверяется отдельно на интервал прибытия и обработки.</p>
          </div>
        </section>
        <section className="panel">
          <div className="panel-h"><h2>Виды ограничений</h2></div>
          <table className="t"><thead><tr><th>Ограничение</th><th>Единица</th><th>Период</th><th>Значение</th><th>Политика</th><th>Правило проверки</th><th>Источник</th></tr></thead>
            <tbody>{d.data.rules.map((r: any) => (
              <tr key={r.code}><td><b>{r.name}</b></td><td>{r.unit}</td><td>{r.period}</td><td>{r.value ?? "—"}</td>
                <td>{r.policy === "hard" ? <Badge cls="bad">жёсткое</Badge> : <Badge cls="warn">целевое</Badge>}</td><td>{r.rule}</td><td className="faint">{r.source}</td></tr>))}</tbody></table>
        </section>
        <section className="panel">
          <div className="panel-h"><h2>Загрузка путей по часам (минут занятости из 60, по резервам)</h2></div>
          <div className="panel-b" style={{ overflow: "auto" }}>
            <table className="t heat"><thead><tr><th>Путь</th>{d.data.hourly.map((h: any) => <th key={h.hour} className="h">{fmtHM(h.hour)}</th>)}</tr></thead>
              <tbody>{Object.entries(d.data.track_labels).map(([tid, label]: any) => (
                <tr key={tid}><td className="nowrap">{label}</td>{d.data.hourly.map((h: any) => {
                  const v = h.tracks[tid] ?? 0;
                  return <td key={h.hour} className="h" style={{ background: `color-mix(in srgb, var(--st-occupied) ${Math.round((v / 60) * 70)}%, transparent)` }} title={`${label}, ${fmtHM(h.hour)}: занято ${v} мин`}>{v || ""}</td>;
                })}</tr>))}</tbody></table>
          </div>
        </section>
        <section className="panel">
          <div className="panel-h"><h2>Ресурсы: занятость на 8 ч (по резервам)</h2></div>
          <table className="t"><thead><tr><th>Ресурс</th><th>Занято, мин</th><th /></tr></thead>
            <tbody>{Object.entries(d.data.resources).map(([rid, r]: any) => (
              <tr key={rid}><td>{r.name}</td><td>{r.busy_min_8h}</td><td style={{ width: "40%" }}><div className="bar"><i style={{ width: `${Math.min(100, r.busy_min_8h / 4.8)}%` }} /></div></td></tr>))}</tbody></table>
        </section>
      </>}
    </div>
  );
}
