import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, Navigate, NavLink, Route, Routes } from "react-router-dom";
import { Copyright, Icon } from "./components/Brand";
import Header from "./components/Header";
import { ForecastBar, ReplayBar } from "./components/Panels";
import { Loading, Toasts } from "./components/ui";
import { api, getToken, setToken } from "./lib/api";
import { setTimezone } from "./lib/format";
import { can, useStore } from "./lib/store";
import { connect, disconnect, reconnectNow } from "./lib/ws";
import Admin from "./pages/Admin";
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
import Work from "./pages/Work";

// мобильный раздел работников — отдельный чанк: без 3D и без полного снимка станции
const MobileApp = lazy(() => import("./mobile/MobileApp"));

function Nav() {
  const live = useStore((s) => s.live);
  const user = useStore((s) => s.user);
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem("ds_nav") === "1"; } catch { return false; } });
  const toggle = () => setCollapsed((c) => { try { localStorage.setItem("ds_nav", c ? "0" : "1"); } catch { /* */ } return !c; });
  const nConf = live ? Object.keys(live.conflicts).length : 0;
  const nAlerts = live ? Object.keys(live.alerts).length : 0;
  const nReq = live ? Object.values(live.requests).filter((r: any) => ["new", "checked"].includes(r.status)).length : 0;
  const nDef = live ? Object.values(live.trains).reduce((a: number, t: any) => a + (t.defects || []).filter((d: any) => d.status === "submitted").length, 0) : 0;
  const link = (to: string, label: string, icon: string, badge?: React.ReactNode) => (
    <NavLink to={to} end className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`} title={collapsed ? label : undefined}>
      <Icon name={icon} /><span className="nav-label">{label}</span>{badge}
    </NavLink>
  );
  return (
    <nav className={`app-nav ${collapsed ? "collapsed" : ""}`} aria-label="Разделы">
      <div className="nav-group">Работа</div>
      {link("/", "Обзор станции", "overview", nConf ? <span className="badge bad">⚠ {nConf}</span> : null)}
      {link("/requests", "Заявки", "requests", nReq ? <span className="badge info">{nReq}</span> : null)}
      {link("/schedule", "Расписание", "schedule")}
      {link("/plan", "План и Гант", "plan")}
      <div className="nav-group">Данные</div>
      {link("/load", "Загрузка и индекс", "load")}
      {link("/devices", "Датчики и связь", "devices", nAlerts ? <span className="badge unknown">? {nAlerts}</span> : null)}
      {user?.permissions?.includes("defect.view_station") && link("/work", "Сообщения работников", "work", nDef ? <span className="badge warn">{nDef}</span> : null)}
      {link("/journal", "Журнал", "journal")}
      {link("/assistant", "Помощник", "assistant")}
      <div className="nav-group">Система</div>
      {link("/settings", "Настройки", "settings")}
      {/* разделы администратора не показываются другим ролям */}
      {user?.permissions?.includes("users.manage") && link("/admin", "Пользователи и роли", "admin")}
      {user?.permissions?.includes("api.docs") && (
        <a className="nav-link" href="/docs" target="_blank" rel="noreferrer" title={collapsed ? "API (Swagger)" : undefined} onClick={async (e) => {
          e.preventDefault();
          const w = window.open("about:blank", "_blank");
          try { await api.postPlain("/api/v1/auth/docs-session"); if (w) w.location.href = "/docs"; }
          catch { w?.close(); useStore.getState().toast("error", "Описание API недоступно"); }
        }}><Icon name="api" /><span className="nav-label">API (Swagger)</span></a>
      )}
      <div className="grow" />
      <a className="nav-link nav-mobile" href="/mobile" title="Мобильное приложение работников ПТО"><Icon name="mobile" /><span className="nav-label">Мобильное приложение</span></a>
      <button className="nav-collapse" onClick={toggle} aria-label={collapsed ? "Развернуть меню" : "Свернуть меню"} title={collapsed ? "Развернуть меню" : "Свернуть меню"}>
        <Icon name={collapsed ? "expand" : "collapse"} /><span className="nav-label">Свернуть меню</span>
      </button>
    </nav>
  );
}

function Shell() {
  const conn = useStore((s) => s.conn);
  const live = useStore((s) => s.live);
  const mode = useStore((s) => s.mode);
  const user = useStore((s) => s.user);
  const setTopology = useStore((s) => s.setTopology);
  const stationKey = live ? `${live.meta.station_id}-${live.meta.scenario}-${live.meta.seed}` : "";
  useEffect(() => { connect(); return () => disconnect(); }, []);
  useEffect(() => {
    if (!live) return;
    setTimezone(live.meta.timezone);
    // защита от поздних ответов: ответ для прежней станции/сценария не применяется
    let actual = true;
    const sid = live.meta.station_id;
    setTopology(null);
    useStore.getState().setNetwork(null);
    api.get("/api/v1/topology").then((t) => { if (actual && t?.station?.id === sid) setTopology(t); }).catch(() => actual && setTopology(null));
    api.get("/api/v1/network")
      .then((n) => { if (actual && n?.main_station_id === sid) useStore.getState().setNetwork(n); })
      .catch((e) => { if (actual) useStore.getState().setNetwork(null, e?.message || "Модель сети недоступна"); });
    return () => { actual = false; };
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
      <div className="app-body">
      <Nav />
      <main id="main" className="app-main" tabIndex={-1}>
        {/* панели режимов — на уровне приложения: «История» и «Прогноз» работают на всех разделах */}
        {live && mode === "history" && <div style={{ padding: "10px 10px 0" }}><ReplayBar /></div>}
        {live && mode === "forecast" && <div style={{ padding: "10px 10px 0" }}><ForecastBar st={live} /></div>}
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
            <Route path="/work" element={can(user, "defect.view_station") ? <Work /> : <Navigate to="/" replace />} />
            <Route path="/admin" element={can(user, "users.manage") ? <Admin /> : <Navigate to="/" replace />} />
            <Route path="*" element={<Overview />} />
          </Routes>
        )}
      </main>
      </div>
      <footer className="app-footer"><Copyright /></footer>
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
  const mobile = location.pathname.startsWith("/mobile");
  if (mobile) {
    return (
      <BrowserRouter>
        <Suspense fallback={<div className="state-box">Загрузка…</div>}>
          <Routes><Route path="/mobile/*" element={<MobileApp />} /></Routes>
        </Suspense>
      </BrowserRouter>
    );
  }
  // рабочие роли без доступа к состоянию станции работают в мобильном разделе
  if (user && !can(user, "state.view") && can(user, "mobile.access")) {
    location.replace("/mobile");
    return null;
  }
  return (
    <BrowserRouter>
      {user ? <Shell /> : <Login />}
      <Toasts />
    </BrowserRouter>
  );
}
