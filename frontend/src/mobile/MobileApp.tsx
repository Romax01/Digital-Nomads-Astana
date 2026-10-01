// Мобильный раздел работников /mobile — дополнение к основному приложению на ПК: тот же backend
// и та же БД, телефон подключается к компьютеру диспетчера по сети. Не загружает 3D и полный
// снимок станции: только свои сообщения, задания и очередь своей зоны (по правам на сервере).
import { useEffect, useState } from "react";
import { Navigate, NavLink, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { api, ApiError, getToken, setToken } from "../lib/api";
import { DefectCard, WorkCard, Chip, DEFECT_CLS, WO_CLS, URG_CLS, URG_ICON } from "./cards";
import { ago, clearImageCache, connectWork, disconnectWork, fmtDT, useM, usePerm } from "./mlib";
import { listItems, otherUsersPending, OutItem, refreshCount, retryItem, delItem, syncNow } from "./outbox";
import ReportForm from "./ReportForm";
import "./mobile.css";
import { Copyright } from "../components/Brand";

const ROLE_HINT: Record<string, string> = {
  wagon_inspector: "Осмотр, сообщения о дефектах, назначенные осмотры",
  wagon_inspector_repairer: "Осмотр и выполнение назначенного ремонта",
  pto_operator: "Приём и маршрутизация сообщений ПТО",
  rolling_stock_fitter: "Задания своей бригады",
  senior_wagon_inspector: "Распределение заданий и контрольный осмотр",
  station_dispatcher: "Очередь сообщений и решения",
};

function MLogin({ onDone }: { onDone: () => void }) {
  const [users, setUsers] = useState<any[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.get("/api/v1/auth/demo-users").then((u) => setUsers(u.filter((x: any) => ROLE_HINT[x.role]))).catch(() => undefined); }, []);
  const go = async (u = username, p = password) => {
    setBusy(true); setErr(null);
    try { const r = await api.postPlain("/api/v1/auth/login", { username: u, password: p }); setToken(r.token); onDone(); }
    catch (e) { setErr((e as ApiError).message); } finally { setBusy(false); }
  };
  return (
    <div className="m-login m-scope">
      <h1>Цифровая станция</h1>
      <p className="m-muted">Мобильное рабочее место работников ПТО. Подключение к основному приложению станции на ПК.</p>
      <form className="m-col" onSubmit={(e) => { e.preventDefault(); go(); }}>
        <label className="m-f">Логин<input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoCapitalize="none" /></label>
        <label className="m-f">Пароль<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>
        {err && <div className="m-err" role="alert">{err}</div>}
        <button className="m-btn primary big" disabled={busy}>Войти</button>
      </form>
      {users.length > 0 && <>
        <h3>Демо-учётные записи (пароль demo123)</h3>
        {users.map((u) => (
          <button key={u.username} className="m-list-item" onClick={() => { setUsername(u.username); setPassword("demo123"); go(u.username, "demo123"); }}>
            <b>{u.full_name}</b><small>{u.role_label} · {ROLE_HINT[u.role]}</small>
          </button>
        ))}
      </>}
      <small className="m-muted">Роли и права назначает администратор. Демонстрационная система поддержки решений: действия не являются командами железнодорожной автоматике.</small>
      <div className="m-copy-in"><Copyright compact /></div>
    </div>
  );
}

function StatusBar() {
  const ctx = useM((s) => s.ctx);
  const online = useM((s) => s.online);
  const ws = useM((s) => s.ws);
  const sync = useM((s) => s.sync);
  return (
    <header className="m-top">
      <div><b>{ctx?.scope.station_name}</b><small>{ctx?.user.role_label}{ctx?.scope.brigade_id ? ` · бригада ${ctx.scope.brigade_id}` : ""}</small></div>
      <div className="m-status" role="status">
        <span className={`m-dot ${!online ? "bad" : ws === "online" ? "ok" : "warn"}`} />
        {!online ? "Нет сети" : ws === "online" ? "На связи" : "Подключение…"}
        {sync.pending > 0 && <span className="m-pending">{sync.sending ? "⇅ отправляется" : `⏳ ${sync.pending} ждёт отправки`}</span>}
      </div>
    </header>
  );
}

function Home() {
  const ctx = useM((s) => s.ctx)!;
  const seq = useM((s) => s.seq);
  const nav = useNavigate();
  const canReport = usePerm("defect.create");
  const canExec = usePerm("work_order.execute");
  const canQueue = usePerm("defect.view_station");
  const [tasks, setTasks] = useState<any[]>([]);
  const [info, setInfo] = useState<any[]>([]);
  useEffect(() => {
    if (canExec) api.get("/api/v1/work-orders?scope=mine&status=active&page_size=10").then((r) => setTasks(r.items)).catch(() => undefined);
    if (canReport) api.get("/api/v1/defect-reports?scope=own&status=needs_info").then((r) => setInfo(r.items)).catch(() => undefined);
  }, [seq]);
  const c = ctx.counts;
  return (
    <div className="m-col">
      <section className="m-hello">
        <h2>{ctx.user.full_name}</h2>
        <small>{ctx.user.role_label} · {ctx.scope.station_name}{ctx.scope.pto_id ? ` · ${ctx.scope.pto_id}` : ""}</small>
        {!ctx.scope.explicit && <div className="m-warn">Привязка к станции не задана администратором — используется основная станция.</div>}
      </section>
      {canReport && <button className="m-btn primary big" onClick={() => nav("/mobile/report")}>＋ Сообщить о дефекте</button>}
      <div className="m-tiles">
        {canExec && <button className="m-tile" onClick={() => nav("/mobile/tasks")}><b>{c.my_active}</b><small>мои активные задания</small></button>}
        {canReport && <button className={`m-tile ${c.needs_info ? "warn" : ""}`} onClick={() => nav("/mobile/tasks?tab=own")}><b>{c.needs_info}</b><small>ждут уточнения</small></button>}
        {canQueue && <button className={`m-tile ${c.queue ? "warn" : ""}`} onClick={() => nav("/mobile/tasks?tab=queue")}><b>{c.queue}</b><small>в очереди сообщений</small></button>}
        {usePerm("work_order.inspect") && <button className={`m-tile ${c.awaiting_inspection ? "warn" : ""}`} onClick={() => nav("/mobile/tasks?tab=inspect")}><b>{c.awaiting_inspection}</b><small>ожидают осмотра</small></button>}
        <button className={`m-tile ${c.unread ? "info" : ""}`} onClick={() => nav("/mobile/notifications")}><b>{c.unread}</b><small>новых уведомлений</small></button>
      </div>
      {info.length > 0 && <section><h3>Запрошено уточнение</h3>{info.map((d) => <DefectRow key={d.id} d={d} />)}</section>}
      {tasks.length > 0 && <section><h3>Активные задания</h3>{tasks.map((w) => <WorkRow key={w.id} w={w} />)}</section>}
      <Outbox />
    </div>
  );
}

function DefectRow({ d }: { d: any }) {
  const nav = useNavigate();
  return (
    <button className="m-list-item" onClick={() => nav(`/mobile/defect/${d.id}`)}>
      <div className="m-row between"><b>№ {d.number} · вагон № {d.wagon_number}</b><Chip cls={DEFECT_CLS[d.status]}>{d.status_label}</Chip></div>
      <small><span className={`m-u ${URG_CLS[d.urgency]}`}>{URG_ICON[d.urgency]} {d.urgency_label}</span> · {d.category_label} · {ago(d.created_at)}
        {d.waiting_min != null && d.status === "submitted" ? ` · ждёт приёма ${d.waiting_min} мин` : ""}{!d.wagon_linked ? " · ⚠ не привязано" : ""}{d.restriction_active ? " · ⛔ ограничение" : ""}</small>
    </button>
  );
}

function WorkRow({ w }: { w: any }) {
  const nav = useNavigate();
  return (
    <button className="m-list-item" onClick={() => nav(`/mobile/work/${w.id}`)}>
      <div className="m-row between"><b>№ {w.number} · {w.kind_label}</b><Chip cls={WO_CLS[w.status]}>{w.status_label}</Chip></div>
      <small>Вагон № {w.wagon?.number} · {w.assignee ? w.assignee.name : "не назначен"} · срок {fmtDT(w.due_at)}{w.overdue ? " · ⚠ просрочено" : ""}{!w.place_ready ? " · место не готово" : ""}</small>
    </button>
  );
}

function Outbox() {
  const ctx = useM((s) => s.ctx)!;
  const sync = useM((s) => s.sync);
  const [items, setItems] = useState<OutItem[]>([]);
  const [others, setOthers] = useState(0);
  const reload = () => { listItems(ctx.user.id).then(setItems); otherUsersPending(ctx.user.id).then(setOthers); };
  useEffect(reload, [sync.pending, sync.sending, sync.last]);
  if (!items.length && !others) return null;
  const label: Record<string, string> = { pending: "Ожидает отправки", sending: "Отправляется", error: "Ошибка" };
  return (
    <section className="m-panel">
      <h3>Отправка с этого устройства</h3>
      {items.map((it) => (
        <div key={it.id} className={`m-out ${it.status}`}>
          <b>Вагон № {it.payload.wagon_number} · {label[it.status]}</b>
          <small>{fmtDT(it.createdAt)} · фото: {it.photos.length}{it.error ? ` · ${it.error.replace(/^!/, "")}` : ""}</small>
          {it.status === "error" && <div className="m-row"><button className="m-btn" onClick={async () => { await retryItem(it.id); await syncNow(ctx.user.id); reload(); }}>Повторить</button>
            <button className="m-btn danger" onClick={async () => { if (confirm("Удалить неотправленное сообщение с устройства?")) { await delItem(it.id); await refreshCount(); reload(); } }}>Удалить</button></div>}
        </div>
      ))}
      {items.length > 0 && <button className="m-btn" disabled={sync.sending} onClick={() => syncNow(ctx.user.id)}>Отправить сейчас</button>}
      {others > 0 && <small className="m-muted">На устройстве есть неотправленные сообщения другой учётной записи ({others}). Они отправятся только после её входа.</small>}
    </section>
  );
}

function Tasks() {
  const seq = useM((s) => s.seq);
  const params = new URLSearchParams(location.search);
  const tabs = [
    usePerm("work_order.execute") && ["mine", "Мои задания"],
    usePerm("work_order.inspect") && ["inspect", "На осмотр"],
    usePerm("work_order.assign") && ["assign", "Распределить"],
    usePerm("defect.view_station") && ["queue", "Очередь сообщений"],
    usePerm("defect.view_own") && ["own", "Мои сообщения"],
  ].filter(Boolean) as [string, string][];
  const [tab, setTab] = useState(params.get("tab") && tabs.some((t) => t[0] === params.get("tab")) ? params.get("tab")! : tabs[0]?.[0]);
  const [status, setStatus] = useState("");
  const [urg, setUrg] = useState("");
  const [wagon, setWagon] = useState("");
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setErr(null);
    const urls: Record<string, string> = {
      mine: `/api/v1/work-orders?scope=mine${status ? `&status=${status}` : "&status=active"}`,
      inspect: "/api/v1/work-orders?scope=inspect",
      assign: `/api/v1/work-orders?scope=station${status ? `&status=${status}` : "&status=active"}`,
      queue: `/api/v1/defect-reports?scope=station${status ? `&status=${status}` : "&status=open"}${urg ? `&urgency=${urg}` : ""}${wagon ? `&wagon=${wagon}` : ""}`,
      own: `/api/v1/defect-reports?scope=own${status ? `&status=${status}` : ""}`,
    };
    if (tab) api.get(urls[tab]).then(setData).catch((e) => setErr(e.message));
  }, [tab, status, urg, wagon, seq]);
  if (!tabs.length) return <div className="m-muted">Для вашей роли нет списков заданий.</div>;
  const defectsTab = tab === "queue" || tab === "own";
  return (
    <div className="m-col">
      <div className="m-tabs" role="tablist">{tabs.map(([k, v]) => <button key={k} role="tab" aria-selected={tab === k} onClick={() => { setTab(k); setStatus(""); }}>{v}</button>)}</div>
      <div className="m-row wrap">
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Статус">
          <option value="">{defectsTab ? (tab === "queue" ? "Открытые" : "Все") : "Активные"}</option>
          {Object.entries(useM.getState().ctx!.dictionaries[defectsTab ? "defect_status" : "work_status"]).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        {tab === "queue" && <>
          <select value={urg} onChange={(e) => setUrg(e.target.value)} aria-label="Срочность"><option value="">Любая срочность</option>
            {Object.entries(useM.getState().ctx!.dictionaries.urgency).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
          <input value={wagon} onChange={(e) => setWagon(e.target.value.replace(/\D/g, ""))} placeholder="Вагон" inputMode="numeric" style={{ width: 110 }} />
        </>}
      </div>
      {err && <div className="m-err">{err}</div>}
      {!data ? <div className="m-loading">Загрузка…</div> : data.items.length === 0 ? <div className="m-empty">Нет записей</div>
        : data.items.map((x: any) => defectsTab ? <DefectRow key={x.id} d={x} /> : <WorkRow key={x.id} w={x} />)}
      {data && data.total > data.items.length && <small className="m-muted">Показано {data.items.length} из {data.total}</small>}
    </div>
  );
}

function Notifications() {
  const seq = useM((s) => s.seq);
  const nav = useNavigate();
  const [data, setData] = useState<any>(null);
  useEffect(() => { api.get("/api/v1/notifications").then(setData).catch(() => undefined); }, [seq]);
  const open = async (n: any) => {
    if (!n.read) await api.postPlain("/api/v1/notifications/read", { ids: [n.id] }).catch(() => undefined);
    nav(n.entity_type === "defect" ? `/mobile/defect/${n.entity_id}` : `/mobile/work/${n.entity_id}`);
  };
  if (!data) return <div className="m-loading">Загрузка…</div>;
  return (
    <div className="m-col">
      <div className="m-row between"><h2>Уведомления</h2>
        {data.unread > 0 && <button className="m-btn ghost" onClick={() => api.postPlain("/api/v1/notifications/read", { all: true }).then(() => useM.getState().bump())}>Прочитать все</button>}</div>
      {data.items.length === 0 && <div className="m-empty">Уведомлений нет</div>}
      {data.items.map((n: any) => (
        <button key={n.id} className={`m-list-item ${n.read ? "" : "unread"}`} onClick={() => open(n)}>
          <b>{n.title}</b><small>{n.body}</small><small className="m-muted">{ago(n.created_at)}</small>
        </button>
      ))}
    </div>
  );
}

function Profile({ onLogout }: { onLogout: () => void }) {
  const ctx = useM((s) => s.ctx)!;
  const sync = useM((s) => s.sync);
  return (
    <div className="m-col">
      <section className="m-panel">
        <h2>{ctx.user.full_name}</h2>
        <div className="m-kv">
          <span>Логин</span><b>{ctx.user.username}</b>
          <span>Роль</span><b>{ctx.user.role_label}</b>
          <span>Станция</span><b>{ctx.scope.station_name}</b>
          <span>ПТО / зона</span><b>{ctx.scope.pto_id ?? "—"}</b>
          <span>Бригада</span><b>{ctx.scope.brigade_id ?? "—"}</b>
          <span>Последняя синхронизация</span><b>{fmtDT(sync.last)}</b>
        </div>
        <small className="m-muted">Роли и привязку назначает администратор в основном приложении.</small>
      </section>
      <section className="m-panel">
        <h3>Приложение</h3>
        <p className="m-muted">Работает в обычном мобильном браузере. Можно добавить на главный экран («Установить приложение» / «На экран „Домой“»). Отправка неотправленных сообщений выполняется при открытии приложения и восстановлении связи; постоянная фоновая работа не обещается.</p>
        <button className="m-btn" onClick={() => location.assign("/")}>Открыть основное приложение (ПК)</button>
      </section>
      <button className="m-btn danger big" onClick={onLogout}>Выйти</button>
    </div>
  );
}

function DefectScreen() {
  const { id } = useParams();
  const nav = useNavigate();
  return <DefectCard id={id!} onOpenWork={(w) => nav(`/mobile/work/${w}`)} />;
}
function WorkScreen() {
  const { id } = useParams();
  const nav = useNavigate();
  return <WorkCard id={id!} onOpenDefect={(r) => nav(`/mobile/defect/${r}`)} />;
}

export default function MobileApp() {
  const ctx = useM((s) => s.ctx);
  const toast = useM((s) => s.toast);
  const needLogin = useM((s) => s.sync.needLogin);
  const nav = useNavigate();
  const [state, setState] = useState<"loading" | "login" | "ready" | "denied">(getToken() ? "loading" : "login");
  const [denied, setDenied] = useState("");
  const loadCtx = async () => {
    try {
      const c = await api.get("/api/v1/mobile/context");
      const prev = useM.getState().ctx?.user.id;
      if (prev && prev !== c.user.id) clearImageCache();
      useM.getState().setCtx(c);
      useM.getState().setSync({ needLogin: false });
      setState("ready");
      connectWork();
      refreshCount(c.user.id);
      syncNow(c.user.id).catch(() => undefined);
    } catch (e) {
      const err = e as ApiError;
      if (err.status === 401) { setToken(null); setState("login"); }
      else if (err.status === 403) { setDenied(err.message); setState("denied"); }
      else if (useM.getState().ctx) setState("ready"); // без связи — работаем с последним контекстом
      else setTimeout(loadCtx, 3000);
    }
  };
  useEffect(() => {
    document.title = "Цифровая станция — работники";
    if (getToken()) loadCtx();
    const on = () => { useM.getState().setOnline(true); loadCtx(); };
    const off = () => useM.getState().setOnline(false);
    window.addEventListener("online", on); window.addEventListener("offline", off);
    const t = setInterval(() => { const c = useM.getState().ctx; if (c && navigator.onLine && useM.getState().sync.pending) syncNow(c.user.id).catch(() => undefined); }, 30000);
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => undefined);
    return () => { window.removeEventListener("online", on); window.removeEventListener("offline", off); clearInterval(t); disconnectWork(); };
  }, []);
  // счётчики главного экрана — по событиям сервера
  const seq = useM((s) => s.seq);
  useEffect(() => { if (state === "ready") api.get("/api/v1/mobile/context").then((c) => useM.getState().setCtx(c)).catch(() => undefined); }, [seq]);
  const logout = () => { disconnectWork(); setToken(null); useM.getState().setCtx(null); clearImageCache(); setState("login"); nav("/mobile"); };

  if (state === "login" || needLogin) {
    return <MLogin onDone={() => { useM.getState().setSync({ needLogin: false }); setState("loading"); loadCtx(); }} />;
  }
  if (state === "denied") {
    return (
      <div className="m-login m-scope">
        <h1>Нет доступа</h1>
        <p>{denied}</p>
        <p className="m-muted">Мобильный раздел — для работников ПТО и станционного диспетчера. Работайте в основном приложении.</p>
        <button className="m-btn primary" onClick={() => location.assign("/")}>Основное приложение</button>
        <button className="m-btn" onClick={logout}>Сменить пользователя</button>
        <div className="m-copy-in"><Copyright compact /></div>
      </div>
    );
  }
  if (!ctx) return <div className="m-login m-scope"><div className="m-loading">Подключение к станции…</div></div>;
  const canReport = ctx.permissions.includes("defect.create");
  const canTasks = ["work_order.execute", "work_order.inspect", "work_order.assign", "defect.view_station", "defect.view_own"].some((p) => ctx.permissions.includes(p));
  return (
    <div className="m-app m-scope">
      <StatusBar />
      <main className="m-main">
        <Routes>
          <Route index element={<Home />} />
          <Route path="report" element={canReport ? <ReportForm onSent={() => nav("/mobile")} /> : <Navigate to="/mobile" replace />} />
          <Route path="tasks" element={<Tasks />} />
          <Route path="notifications" element={<Notifications />} />
          <Route path="profile" element={<Profile onLogout={logout} />} />
          <Route path="defect/:id" element={<DefectScreen />} />
          <Route path="work/:id" element={<WorkScreen />} />
          <Route path="*" element={<Navigate to="/mobile" replace />} />
        </Routes>
      </main>
      {toast && <div className={`m-toast ${toast.kind}`} role="status">{toast.text}</div>}
      <div className="m-copy"><Copyright compact /></div>
      <nav className="m-nav" aria-label="Разделы">
        <NavLink to="/mobile" end>⌂<span>Главная</span></NavLink>
        {canReport && <NavLink to="/mobile/report">＋<span>Сообщить</span></NavLink>}
        {canTasks && <NavLink to="/mobile/tasks">☰<span>Задания</span></NavLink>}
        <NavLink to="/mobile/notifications">🔔<span>Уведомления{ctx.counts.unread ? <i className="m-badge">{ctx.counts.unread}</i> : null}</span></NavLink>
        <NavLink to="/mobile/profile">👤<span>Профиль</span></NavLink>
      </nav>
    </div>
  );
}
