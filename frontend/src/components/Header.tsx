import { NotificationsBell } from "../pages/Work";
import { useEffect, useState } from "react";
import { setToken } from "../lib/api";
import { fmtHM, fmtHMS, tzLabel } from "../lib/format";
import { ROLE_LABEL } from "../lib/labels";
import { useStore, useViewState } from "../lib/store";
import { disconnect, reconnectNow } from "../lib/ws";

export function useNow(ms = 1000) {
  const [n, setN] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setN(Date.now()), ms); return () => clearInterval(t); }, [ms]);
  return n;
}

const CAT_CLS: Record<string, string> = { normal: "ok", attention: "warn", critical: "bad", unknown: "unknown" };

export default function Header() {
  const now = useNow();
  const st = useViewState();
  const conn = useStore((s) => s.conn);
  const lastMsgAt = useStore((s) => s.lastMsgAt);
  const mode = useStore((s) => s.mode);
  const setMode = useStore((s) => s.setMode);
  const user = useStore((s) => s.user);
  const theme = useStore((s) => s.theme);
  const setTheme = useStore((s) => s.setTheme);
  const age = lastMsgAt ? (now - lastMsgAt) / 1000 : null;
  const meta = st?.meta;
  const idx = st?.index;
  const connInfo = conn === "online"
    ? (age !== null && age > 5 ? { cls: "warn", text: `Обновления задерживаются (${Math.round(age)} с)` } : { cls: "ok", text: "Подключено" })
    : conn === "reconnecting" ? { cls: "warn", text: "Переподключение…" } : conn === "connecting" ? { cls: "info", text: "Подключение…" }
      : { cls: "bad", text: "Нет соединения" };
  return (
    <header className="app-header">
      <div className="brand">
        <b>Цифровая станция</b>
        <span className="row" style={{ gap: 6 }}>{meta?.station_name ?? "—"} {meta?.is_demo && <span className="demo-flag" title="Схема, ограничения и показатели демонстрационные">ДЕМО</span>}</span>
      </div>
      <div className="hdr-block" aria-label="Модельное время">
        <span className="clock">{fmtHMS(meta?.model_time)}</span>
        <small>модельное время · {tzLabel()}</small>
      </div>
      <div className="hdr-block">
        <span><b title={meta?.real_time_mode ? "Модельное время идёт по часам сервера (×1)" : undefined}>{meta ? (meta.real_time_mode && meta.running ? "● Реальное время" : meta.running ? `▶ Симуляция ×${meta.speed}` : "⏸ Пауза") : "—"}</b></span>
        <small title="Сценарий симуляции">{meta?.scenario_title ?? ""}</small>
      </div>
      <div className="seg" role="group" aria-label="Режим просмотра">
        <button aria-pressed={mode === "live"} onClick={() => setMode("live")}>Сейчас</button>
        <button aria-pressed={mode === "history"} onClick={() => setMode("history")}>История</button>
        <button aria-pressed={mode === "forecast"} onClick={() => setMode("forecast")}>Прогноз</button>
      </div>
      <div className="grow" />
      {idx && (
        <div className="hdr-block" title={`Индекс эффективности: ${idx.formula}. Качество оценки: ${idx.quality.label}`}>
          <span><b className="mono">{idx.value ?? "—"}</b> <span className={`badge ${CAT_CLS[idx.category]}`}>{idx.category_label}</span></span>
          <small>индекс эффективности · расчёт {fmtHM(idx.computed_at)}</small>
        </div>
      )}
      <div className="hdr-block" aria-live="polite">
        <span className={`badge ${connInfo.cls}`}><span aria-hidden>{conn === "online" ? "●" : conn === "offline" ? "✕" : "↻"}</span>{connInfo.text}</span>
        <small>{age !== null ? `данные: ${fmtHMS(new Date(lastMsgAt).toISOString())} (реальное время)` : "данных ещё нет"}</small>
      </div>
      {conn === "offline" && <button className="btn small" onClick={reconnectNow}>Переподключиться</button>}
      {user && <NotificationsBell />}
      <button className="btn ghost small" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Сменить тему">
        {theme === "dark" ? "☀ Светлая" : "☾ Тёмная"}
      </button>
      {user && (
        <div className="hdr-block">
          <span>{user.full_name}</span>
          <small>{user.role_label ?? ROLE_LABEL[user.role]} · <a href="#" onClick={(e) => { e.preventDefault(); disconnect(); setToken(null); useStore.getState().setUser(null); }}>сменить пользователя</a></small>
        </div>
      )}
    </header>
  );
}
