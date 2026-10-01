import { useEffect, useState } from "react";
import { ActionButton, Badge, ErrorBox, Loading, notifyError, useFetch } from "../components/ui";
import { api } from "../lib/api";
import { fmtFull, fmtHM } from "../lib/format";
import { DEVICE_KIND, ROLE_LABEL } from "../lib/labels";
import { can, useStore, useViewState } from "../lib/store";
import { PermissionMatrix } from "./Admin";

export default function Settings() {
  const user = useStore((s) => s.user);
  const st = useViewState();
  const sim = useFetch(() => api.get("/api/v1/sim"), [st?.meta.scenario, st?.meta.running, st?.meta.speed]);
  const perms = useFetch(() => (can(user, "users.manage") ? api.get("/api/v1/permissions") : Promise.resolve(null)), [user?.role]);
  const admin = can(user, "sim.control") ? null : "Управление симуляцией — роль «Администратор».";
  const cfgAdmin = can(user, "config.manage") ? null : "Изменение конфигурации — роль «Администратор».";
  const [scenario, setScenario] = useState("normal");
  const [seed, setSeed] = useState(42);
  const [config, setConfig] = useState("large");
  useEffect(() => { if (sim.data) { setScenario(sim.data.scenario); setSeed(sim.data.seed); setConfig(sim.data.station_config); } }, [sim.data?.scenario]);
  const post = async (url: string, body?: any, ok?: string) => {
    try { await api.postPlain(url, body); if (ok) useStore.getState().toast("ok", ok); sim.reload(); } catch (e) { notifyError(e); }
  };
  return (
    <div className="page">
      <h1>Настройки</h1>
      <section className="panel">
        <div className="panel-h"><h2 className="grow">Симуляция и демонстрационные сценарии</h2>{sim.data && <Badge cls={sim.data.running ? "ok" : "muted"}>{sim.data.running ? `▶ ×${sim.data.speed}` : "⏸ пауза"}</Badge>}</div>
        <div className="panel-b col">
          {!sim.data ? <Loading /> : <>
            <div className="row wrap">
              <ActionButton disabledReason={admin} onClick={() => post("/api/v1/sim/start", undefined, "Симуляция запущена")}>▶ Запустить</ActionButton>
              <ActionButton disabledReason={admin} onClick={() => post("/api/v1/sim/pause", undefined, "Пауза")}>⏸ Пауза</ActionButton>
              {[1, 5, 10, 30, 60].map((s) => <ActionButton key={s} small disabledReason={admin} onClick={() => post("/api/v1/sim/speed", { speed: s }, `Скорость ×${s}`)}>×{s}</ActionButton>)}
              <span className="muted">Модельное время: {fmtFull(sim.data.model_time)}</span>
            </div>
            <div className="form-grid">
              <label className="f">Сценарий<select value={scenario} onChange={(e) => setScenario(e.target.value)}>
                {Object.entries(sim.data.scenarios).map(([k, v]: any) => <option key={k} value={k}>{v.title}</option>)}</select></label>
              <label className="f">Seed<input type="number" value={seed} onChange={(e) => setSeed(+e.target.value)} /></label>
              <label className="f">Станция<select value={config} onChange={(e) => setConfig(e.target.value)}>
                {Object.entries(sim.data.configs).map(([k, v]: any) => <option key={k} value={k}>{v}</option>)}</select></label>
            </div>
            <p className="muted" style={{ margin: 0 }}>{sim.data.scenarios[scenario]?.description}</p>
            <div><ActionButton kind="primary" disabledReason={admin} onClick={() => post("/api/v1/sim/reset", { scenario, seed, station_config: config }, "Сценарий загружен: начальное состояние воспроизведено")}>Сбросить и загрузить сценарий</ActionButton></div>
            {sim.data.scheduled_events?.length > 0 && <div><h4>Запланированные события сценария</h4><ul>{sim.data.scheduled_events.map((e: any, i: number) =>
              <li key={i}>{fmtHM(e.at)} — {e.type === "incident" ? `инцидент ${e.kind}` : e.type === "device_fault" ? `неисправность устройства ${e.device_id} (${e.duration_s} с)` : e.key} {e.fired ? <Badge cls="ok">выполнено</Badge> : <Badge cls="muted">ожидает</Badge>}</li>)}</ul></div>}
          </>}
        </div>
      </section>
      <IndexConfig disabledReason={cfgAdmin} />
      <Policies disabledReason={cfgAdmin} />
      {can(user, "users.manage") && perms.data && <PermissionMatrix data={perms.data} />}
    </div>
  );
}

function IndexConfig({ disabledReason }: { disabledReason: string | null }) {
  const d = useFetch(() => api.get("/api/v1/index/config"), []);
  const [cfg, setCfg] = useState<any>(null);
  const [reason, setReason] = useState("");
  useEffect(() => { if (d.data) setCfg(JSON.parse(JSON.stringify(d.data.active.config))); }, [d.data]);
  if (!d.data || !cfg) return <section className="panel"><Loading /></section>;
  const comps = d.data.components;
  const total = Object.values(cfg.weights).reduce((a: number, b: any) => a + Number(b), 0) as number;
  return (
    <section className="panel">
      <div className="panel-h"><h2 className="grow">Индекс эффективности: веса, нормализация, пороги</h2><span className="muted">активная версия v{d.data.active.version} ({d.data.active.created_by})</span></div>
      <div className="panel-b col">
        <p className="muted" style={{ margin: 0 }}>I = 100 × Σ(wᵢ·sᵢ) / Σwᵢ. Изменение создаёт новую версию конфигурации без перекомпиляции и записывается в аудит.</p>
        <table className="t"><thead><tr><th>Составляющая</th><th>Вес wᵢ</th><th>Доля</th></tr></thead>
          <tbody>{Object.keys(cfg.weights).map((k) => (
            <tr key={k}><td>{comps[k]?.title}<div className="faint">{comps[k]?.source}</div></td>
              <td><input type="number" step={0.05} min={0} value={cfg.weights[k]} aria-label={`Вес: ${comps[k]?.title}`}
                onChange={(e) => setCfg({ ...cfg, weights: { ...cfg.weights, [k]: +e.target.value } })} style={{ width: 90 }} /></td>
              <td>{total > 0 ? `${Math.round((cfg.weights[k] / total) * 100)}%` : "—"}</td></tr>))}</tbody></table>
        <div className="form-grid">
          <label className="f">Порог «Норма» ≥<input type="number" value={cfg.thresholds.normal} onChange={(e) => setCfg({ ...cfg, thresholds: { ...cfg.thresholds, normal: +e.target.value } })} /></label>
          <label className="f">Порог «Внимание» ≥<input type="number" value={cfg.thresholds.attention} onChange={(e) => setCfg({ ...cfg, thresholds: { ...cfg.thresholds, attention: +e.target.value } })} /></label>
          <label className="f">Целевая загрузка путей: от<input type="number" step={0.05} value={cfg.params.track_utilization.target_low} onChange={(e) => setCfg({ ...cfg, params: { ...cfg.params, track_utilization: { ...cfg.params.track_utilization, target_low: +e.target.value } } })} /></label>
          <label className="f">до<input type="number" step={0.05} value={cfg.params.track_utilization.target_high} onChange={(e) => setCfg({ ...cfg, params: { ...cfg.params, track_utilization: { ...cfg.params.track_utilization, target_high: +e.target.value } } })} /></label>
          <label className="f">Необходимый резерв ресурсов (доля)<input type="number" step={0.05} value={cfg.params.idle.reserve_share} onChange={(e) => setCfg({ ...cfg, params: { ...cfg.params, idle: { ...cfg.params.idle, reserve_share: +e.target.value } } })} /></label>
          <label className="f">Цель пропуска, ваг./ч<input type="number" value={cfg.params.throughput.target_wagons_per_hour} onChange={(e) => setCfg({ ...cfg, params: { ...cfg.params, throughput: { ...cfg.params.throughput, target_wagons_per_hour: +e.target.value } } })} /></label>
        </div>
        {total <= 0 && <div className="field-err">Сумма весов должна быть больше нуля.</div>}
        <label className="f">Основание изменения<input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="например: приоритет графика в часы пик" /></label>
        <div><ActionButton kind="primary" disabledReason={disabledReason ?? (total <= 0 ? "Сумма весов должна быть больше нуля." : null)} onClick={async () => {
          try { const r = await api.put("/api/v1/index/config", { ...cfg, reason: reason || "Изменение конфигурации индекса" }); useStore.getState().toast("ok", `Сохранена версия конфигурации v${r.version}`); d.reload(); }
          catch (e) { notifyError(e); }
        }}>Сохранить новую версию</ActionButton></div>
        <details><summary>История версий</summary><ul>{d.data.history.map((h: any) => <li key={h.version}>v{h.version} — {fmtFull(h.created_at)}, {h.created_by} {h.active && <Badge cls="ok">активна</Badge>}</li>)}</ul></details>
      </div>
    </section>
  );
}

function Policies({ disabledReason }: { disabledReason: string | null }) {
  const cap = useFetch(() => api.get("/api/v1/capacity"), []);
  const th = useFetch(() => api.get("/api/v1/config/thresholds"), []);
  const [vals, setVals] = useState<Record<string, number>>({});
  if (!cap.data || !th.data) return <section className="panel"><Loading /></section>;
  const policy = cap.data.plan.policy;
  return (
    <section className="panel">
      <div className="panel-h"><h2>Политики и пороги данных</h2></div>
      <div className="panel-b col">
        <div className="row wrap">
          <span>Месячный план: <b>{policy === "hard_quota" ? "жёсткая квота (превышение запрещает заявки)" : "целевой показатель (превышение — предупреждение)"}</b></span>
          <ActionButton small disabledReason={disabledReason} onClick={async () => {
            try { await api.put("/api/v1/config/plan-policy", { policy: policy === "hard_quota" ? "soft" : "hard_quota", reason: "Смена политики на стенде" }); useStore.getState().toast("ok", "Политика изменена"); cap.reload(); }
            catch (e) { notifyError(e); }
          }}>{policy === "hard_quota" ? "Переключить на «цель»" : "Включить «жёсткую квоту»"}</ActionButton>
        </div>
        <h4>Пороги устаревания по типам источников</h4>
        <table className="t"><thead><tr><th>Тип</th><th>Устройств</th><th>Период, с</th><th>Порог устаревания, с</th><th /></tr></thead>
          <tbody>{th.data.map((t: any) => (
            <tr key={t.kind}><td>{DEVICE_KIND[t.kind] ?? t.kind}</td><td>{t.devices}</td><td>{t.period_s}</td>
              <td><input type="number" min={1} value={vals[t.kind] ?? t.stale_after_s} onChange={(e) => setVals({ ...vals, [t.kind]: +e.target.value })} style={{ width: 90 }} aria-label={`Порог: ${t.kind}`} /></td>
              <td><ActionButton small disabledReason={disabledReason} onClick={async () => {
                try { await api.put("/api/v1/config/device-thresholds", { kind: t.kind, stale_after_s: vals[t.kind] ?? t.stale_after_s }); useStore.getState().toast("ok", "Порог сохранён"); th.reload(); }
                catch (e) { notifyError(e); }
              }}>Сохранить</ActionButton></td></tr>))}</tbody></table>
        {cap.error && <ErrorBox error={cap.error} />}
      </div>
    </section>
  );
}
