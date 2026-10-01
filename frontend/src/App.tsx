import { useEffect } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import Header from "./components/Header";
import { Loading, Toasts } from "./components/ui";
import { api, getToken, setToken } from "./lib/api";
import { setTimezone } from "./lib/format";
import { useStore } from "./lib/store";
import { connect, disconnect, reconnectNow } from "./lib/ws";
import Assistant from "./pages/Assistant";
import Devices from "./pages/Devices";
import Journal from "./pages/Journal";
import Load from "./pages/Load";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import PlanGantt from "./pages/PlanGantt";
import Requests from "./pages/Requests";
import Schedule from "./pages/Schedule";
import Settings from "./pages/Settings";

function Nav() {
  const live = useStore((s) => s.live);
  const nConf = live ? Object.keys(live.conflicts).length : 0;
  const nAlerts = live ? Object.keys(live.alerts).length : 0;
  const nReq = live ? Object.values(live.requests).filter((r: any) => ["new", "checked"].includes(r.status)).length : 0;
  const link = (to: string, label: string, badge?: React.ReactNode) => (
    <NavLink to={to} end className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>{label}{badge}</NavLink>
  );
  return (
    <nav className="app-nav" aria-label="Разделы">
      {link("/", "Обзор станции", nConf ? <span className="badge bad">⚠ {nConf}</span> : null)}
      {link("/requests", "Заявки", nReq ? <span className="badge info">{nReq}</span> : null)}
      {link("/schedule", "Расписание")}
      {link("/plan", "План и Гант")}
      {link("/load", "Загрузка и индекс")}
      {link("/devices", "Датчики и связь", nAlerts ? <span className="badge unknown">? {nAlerts}</span> : null)}
      {link("/journal", "Журнал")}
      {link("/assistant", "Помощник")}
      <div className="nav-sep" />
      {link("/settings", "Настройки")}
      <a className="nav-link" href="/docs" target="_blank" rel="noreferrer">API (Swagger)</a>
    </nav>
  );
}

function Shell() {
  const conn = useStore((s) => s.conn);
  const live = useStore((s) => s.live);
  const setTopology = useStore((s) => s.setTopology);
  const stationKey = live ? `${live.meta.station_id}-${live.meta.scenario}-${live.meta.seed}` : "";
  useEffect(() => { connect(); return () => disconnect(); }, []);
  useEffect(() => {
    if (!live) return;
    setTimezone(live.meta.timezone);
    api.get("/api/v1/topology").then(setTopology).catch(() => setTopology(null));
  }, [stationKey]);
  return (
    <div className="app">
      <a className="skip" href="#main">Перейти к содержимому</a>
      <Header />
      {(conn === "offline" || conn === "reconnecting") && (
        <div className="conn-lost" role="alert">
          <b>{conn === "offline" ? "✕ Нет соединения с сервером." : "↻ Соединение потеряно, переподключение…"}</b>
          <span>Показано последнее полученное состояние{live ? ` (модельное время ${new Date(live.meta.model_time).toLocaleTimeString("ru-RU", { timeZone: "Asia/Almaty" })})` : ""} — оно может быть неактуальным.</span>
          <button className="btn small" onClick={reconnectNow}>Переподключиться</button>
        </div>
      )}
      <Nav />
      <main id="main" className="app-main" tabIndex={-1}>
        {!live ? <Loading text="Подключение к станции…" /> : (
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/requests" element={<Requests />} />
            <Route path="/schedule" element={<Schedule />} />
            <Route path="/plan" element={<PlanGantt />} />
            <Route path="/load" element={<Load />} />
            <Route path="/devices" element={<Devices />} />
            <Route path="/journal" element={<Journal />} />
            <Route path="/assistant" element={<Assistant />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<Overview />} />
          </Routes>
        )}
      </main>
    </div>
  );
}

export default function App() {
  const user = useStore((s) => s.user);
  const theme = useStore((s) => s.theme);
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  useEffect(() => {
    if (getToken() && !user) api.get("/api/v1/auth/me").then((u) => useStore.getState().setUser(u)).catch(() => setToken(null));
  }, []);
  return (
    <BrowserRouter>
      {user ? <Shell /> : <Login />}
      <Toasts />
    </BrowserRouter>
  );
}
