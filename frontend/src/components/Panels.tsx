import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { fmtHM, fmtHMS, fmtMin, ms } from "../lib/format";
import { applyDelta } from "../lib/reducer";
import { SEVERITY } from "../lib/labels";
import { can, useStore } from "../lib/store";
import type { IndexState, ViewState } from "../lib/types";
import { ActionButton, Badge, Empty, ErrorBox, Loading, notifyError } from "./ui";

export function ConflictsList({ st }: { st: ViewState }) {
  const select = useStore((s) => s.select);
  const list = Object.values(st.conflicts);
  if (!list.length) return <Empty text="Конфликтов нет" hint="Прогнозный план не нарушает жёстких ограничений." />;
  return (
    <div role="list">
      {list.map((c) => (
        <div key={c.id} role="listitem" className="item clickable" tabIndex={0}
          onClick={() => select({ type: "conflict", id: c.id })} onKeyDown={(e) => e.key === "Enter" && select({ type: "conflict", id: c.id })}>
          <div className="title"><span className={SEVERITY[c.severity].cls}>{SEVERITY[c.severity].icon}</span>{c.title}
            <span className={`badge ${c.severity === "critical" ? "bad" : c.severity === "high" ? "warn" : "muted"}`} style={{ marginLeft: "auto" }}>{c.severity_label}</span></div>
          <div>{c.explanation}</div>
          <div className="faint">{c.start ? `с ${fmtHM(c.start)}` : ""}{c.end ? ` до ${fmtHM(c.end)}` : ""} · обнаружен {fmtHM(c.detected_at)}</div>
        </div>
      ))}
    </div>
  );
}

export function RecommendationsList({ st }: { st: ViewState }) {
  const user = useStore((s) => s.user);
  const mode = useStore((s) => s.mode);
  const nav = useNavigate();
  const [computing, setComputing] = useState(false);
  const recs = Object.values(st.recommendations).sort((a, b) => ms(b.computed_real_at) - ms(a.computed_real_at));
  const compute = async () => {
    setComputing(true);
    try { await api.postPlain("/api/v1/plans/compute"); useStore.getState().toast("ok", "План пересчитан"); }
    catch (e) { notifyError(e); } finally { setComputing(false); }
  };
  const computeReason = mode === "history" ? "Недоступно в режиме «История»." : can(user, "plan.compute") ? null : "Пересчёт доступен диспетчерам и дежурному.";
  return (
    <div>
      <div className="item">
        <div className="row between wrap">
          <span className="muted">Рекомендации рассчитываются сервером: детектор конфликтов + планировщик CP-SAT.</span>
          <ActionButton small busy={computing} disabledReason={computeReason} onClick={compute}>Пересчитать</ActionButton>
        </div>
      </div>
      {!recs.length && <Empty text="Рекомендаций нет" hint="Появятся автоматически при конфликтах в плане." />}
      {recs.map((r) => {
        const stale = r.status === "stale";
        const reason = mode === "history" ? "Недоступно в режиме «История»." : stale ? "Рекомендация устарела: состояние изменилось после расчёта. Нажмите «Пересчитать»." :
          r.status !== "active" ? `Статус: ${r.status_label}` : !can(user, "plan.apply") ? "Применение плана — станционный диспетчер." : null;
        return (
          <div key={r.id} className="item">
            <div className="title">💡 {r.title} <Badge cls={stale ? "warn" : r.status === "applied" ? "ok" : "info"}>{r.status_label}</Badge></div>
            <div><b>Причина:</b> {r.reason}</div>
            {r.effect && (
              <div className="kv" style={{ fontSize: 13 }}>
                <span className="muted">Конфликты</span><span>{r.effect.conflicts_before} → <b>{r.effect.conflicts_after}</b></span>
                <span className="muted">Задержка</span><span>{fmtMin(r.effect.delay_before_min)} → <b>{fmtMin(r.effect.delay_after_min)}</b></span>
                <span className="muted">Индекс (прогноз)</span><span>{r.effect.index_before ?? "—"} → <b>{r.effect.index_after ?? "—"}</b></span>
                <span className="muted">Решение</span><span>{r.effect.solver_status}, {Math.round(r.effect.solve_ms)} мс</span>
              </div>
            )}
            <div className="faint">Затронуто: {r.affected.slice(0, 6).map((a) => a.label).join(", ")}</div>
            <div className="faint">Актуальность расчёта: {fmtHMS(r.computed_at)} (модельное), версия состояния {r.based_on_version}</div>
            <div className="row wrap">
              <button className="btn small" onClick={() => nav("/plan")}>Сравнить планы</button>
              <ActionButton small kind="primary" disabledReason={reason} onClick={async () => {
                try {
                  const res = await api.post(`/api/v1/recommendations/${r.id}/apply`);
                  useStore.getState().toast("ok", `План применён: изменено операций — ${res.changed_operations}`, `Поезда: ${res.trains.join(", ")}`);
                } catch (e) { notifyError(e); }
              }}>Применить</ActionButton>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function AlertsList({ st }: { st: ViewState }) {
  const select = useStore((s) => s.select);
  const list = Object.values(st.alerts);
  if (!list.length) return <Empty text="Проблем датчиков, влияющих на работу, нет" />;
  return (
    <div>
      {list.map((a) => (
        <div key={a.id} className="item clickable" tabIndex={0}
          onClick={() => select({ type: a.object_type === "track" ? "track" : "device", id: a.object_id } as any)}
          onKeyDown={(e) => e.key === "Enter" && select({ type: "track", id: a.object_id })}>
          <div className="title"><span className="unknown">?</span>{a.message}</div>
          {a.affected_operations.length > 0 && <div className="faint">Зависимых операций: {a.affected_operations.length} — подтверждение заблокировано</div>}
        </div>
      ))}
    </div>
  );
}

const CAT_CLS: Record<string, string> = { normal: "ok", attention: "warn", critical: "bad", unknown: "unknown" };

export function IndexWidget({ idx, onOpen }: { idx: IndexState | null; onOpen?: () => void }) {
  if (!idx) return <div className="kpi"><small>Индекс эффективности</small><b>—</b><small>расчёт…</small></div>;
  const trend = (idx.trend ?? []).filter((p) => p.v !== null) as { v: number }[];
  const W = 120, H = 28;
  const pts = trend.map((p, i) => `${(i / Math.max(1, trend.length - 1)) * W},${H - (p.v / 100) * H}`).join(" ");
  return (
    <div className={`kpi ${onOpen ? "click" : ""}`} onClick={onOpen} role={onOpen ? "button" : undefined} tabIndex={onOpen ? 0 : undefined}
      title={idx.factors.map((f) => `${f.title}: −${f.loss_points} балл.`).join("\n")}>
      <small>Индекс эффективности</small>
      <div className="row"><b className="mono">{idx.value ?? "—"}</b><span className={`badge ${CAT_CLS[idx.category]}`}>{idx.category_label}</span>
        {trend.length > 1 && <svg width={W} height={H} aria-label="Динамика индекса"><polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth={1.6} /></svg>}</div>
      <small>Качество оценки: {idx.quality.label}{idx.quality.missing.length ? ` (нет: ${idx.quality.missing.join(", ")})` : ""}</small>
      {idx.factors[0] && <small>Снижает: {idx.factors[0].title.toLowerCase()} (−{idx.factors[0].loss_points})</small>}
    </div>
  );
}

/** Перемотка последних минут: снимок + дельты, тот же редьюсер. Ничего не меняет в текущей станции. */
export function ReplayBar() {
  const replay = useStore((s) => s.replay);
  const setReplay = useStore((s) => s.setReplay);
  const setMode = useStore((s) => s.setMode);
  const [minutes, setMinutes] = useState(10);
  const [err, setErr] = useState<ApiError | null>(null);
  const load = async (mins = minutes) => {
    setReplay({ loading: true, playing: false, frames: [], index: 0 }); setErr(null);
    try {
      const to = new Date(), from = new Date(to.getTime() - mins * 60e3);
      const r = await api.get(`/api/v1/replay/window?from=${from.toISOString()}&to=${to.toISOString()}`);
      let state = r.snapshot.state;
      const frames = [{ real_time: r.snapshot.real_time, model_time: r.snapshot.model_time, state }];
      for (const d of r.deltas) { state = applyDelta(state, d.delta); frames.push({ real_time: d.real_time, model_time: d.model_time, state }); }
      const startIdx = Math.max(0, frames.findIndex((f) => ms(f.real_time) >= from.getTime()));
      setReplay({ frames, index: startIdx, loading: false });
    } catch (e) { setErr(e as ApiError); setReplay({ loading: false }); }
  };
  useEffect(() => { load(); }, []);
  const timer = useRef<number>();
  useEffect(() => {
    clearInterval(timer.current);
    if (replay.playing) {
      timer.current = window.setInterval(() => {
        const s = useStore.getState().replay;
        if (s.index >= s.frames.length - 1) { setReplay({ playing: false }); return; }
        setReplay({ index: s.index + 1 });
      }, 1000 / replay.speed);
    }
    return () => clearInterval(timer.current);
  }, [replay.playing, replay.speed]);
  const f = replay.frames[replay.index];
  return (
    <div className="mode-banner history" role="region" aria-label="Перемотка истории">
      <span>⏪ История — только просмотр, команды недоступны</span>
      {replay.loading && <Loading text="Загрузка истории…" />}
      {err && <ErrorBox error={err} retry={() => load()} />}
      {replay.frames.length > 0 && <>
        <label className="row">Окно
          <select value={minutes} onChange={(e) => { setMinutes(+e.target.value); load(+e.target.value); }}>
            {[5, 10, 15].map((m) => <option key={m} value={m}>{m} мин</option>)}
          </select></label>
        <button className="btn small" onClick={() => setReplay({ playing: !replay.playing })}>{replay.playing ? "⏸ Пауза" : "▶ Воспроизвести"}</button>
        <select aria-label="Скорость воспроизведения" value={replay.speed} onChange={(e) => setReplay({ speed: +e.target.value })}>
          {[1, 2, 5, 10].map((s) => <option key={s} value={s}>×{s}</option>)}
        </select>
        <input type="range" min={0} max={replay.frames.length - 1} value={replay.index} aria-label="Момент истории"
          onChange={(e) => setReplay({ index: +e.target.value, playing: false })} style={{ flex: 1, minWidth: 160 }} />
        <span className="mono">{fmtHMS(f?.real_time)} реальн. · {fmtHMS(f?.model_time)} модельн.</span>
        <button className="btn small primary" onClick={() => { setReplay({ playing: false }); setMode("live"); }}>Вернуться к текущему</button>
      </>}
    </div>
  );
}

export function ForecastBar({ st }: { st: ViewState }) {
  const at = useStore((s) => s.forecastAt);
  const setAt = useStore((s) => s.setForecastAt);
  const setMode = useStore((s) => s.setMode);
  const base = ms(st.meta.model_time);
  const offset = at ? Math.round((ms(at) - base) / 60e3) : 60;
  useEffect(() => { if (!at) setAt(new Date(base + 60 * 60e3).toISOString()); }, []);
  return (
    <div className="mode-banner forecast" role="region" aria-label="Прогноз">
      <span>🔮 Прогноз на {fmtHM(at ?? undefined)} — по плановым и прогнозным операциям, не наблюдение датчиков</span>
      <input type="range" min={15} max={480} step={15} value={offset} aria-label="Горизонт прогноза, минут"
        onChange={(e) => setAt(new Date(base + +e.target.value * 60e3).toISOString())} style={{ flex: 1, minWidth: 160 }} />
      <span className="mono">+{fmtMin(offset)}</span>
      <button className="btn small primary" onClick={() => setMode("live")}>Вернуться к текущему</button>
    </div>
  );
}
