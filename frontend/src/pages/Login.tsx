import { useEffect, useState } from "react";
import { api, ApiError, setToken } from "../lib/api";
import { ROLE_LABEL } from "../lib/labels";
import { useStore } from "../lib/store";

export default function Login() {
  const [users, setUsers] = useState<any[]>([]);
  const [username, setUsername] = useState("duty");
  const [password, setPassword] = useState("demo123");
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.get("/api/v1/auth/demo-users").then(setUsers).catch(() => setUsers([])); }, []);
  const submit = async (u = username) => {
    setBusy(true); setErr(null);
    try {
      const r = await api.postPlain("/api/v1/auth/login", { username: u, password });
      setToken(r.token);
      const me = await api.get("/api/v1/auth/me");
      useStore.getState().setUser(me);
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  return (
    <div className="login-wrap">
      <main className="panel login">
        <div className="panel-h"><h1 className="grow">Цифровая станция</h1><span className="demo-flag">ДЕМО</span></div>
        <div className="panel-b col">
          <p className="muted" style={{ margin: 0 }}>
            Учебно-демонстрационная система поддержки решений диспетчера. Данные синтетические; система не управляет
            стрелками, сигналами и движением поездов и не является сертифицированной системой обеспечения безопасности.
          </p>
          <form className="col" onSubmit={(e) => { e.preventDefault(); submit(); }}>
            <label className="f">Пользователь<input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" /></label>
            <label className="f">Пароль<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" /></label>
            {err && <div className="callout bad" role="alert">{err.message}</div>}
            <button className="btn primary" type="submit" disabled={busy}>Войти</button>
          </form>
          <h4 style={{ marginTop: 8 }}>Демонстрационные роли (пароль demo123)</h4>
          <div className="col" style={{ gap: 4 }}>
            {users.map((u) => (
              <button key={u.username} className="btn" style={{ justifyContent: "space-between" }} onClick={() => { setUsername(u.username); submit(u.username); }}>
                <span><b>{u.username}</b> — {u.full_name}</span><span className="muted">{u.role_label ?? ROLE_LABEL[u.role]}</span>
              </button>
            ))}
            {!users.length && <span className="muted">Список ролей недоступен — backend ещё запускается.</span>}
          </div>
        </div>
      </main>
    </div>
  );
}
