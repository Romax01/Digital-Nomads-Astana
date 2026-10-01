import { useState } from "react";
import { ActionButton, Badge, Empty, ErrorBox, Loading, Modal, notifyError, useFetch } from "../components/ui";
import { api, ApiError, newKey } from "../lib/api";
import { can, useStore } from "../lib/store";

/** Пользователи и роли. Изменения доступны только системной роли «Администратор» (проверяется сервером). */
export default function Admin() {
  const user = useStore((s) => s.user);
  const isAdmin = can(user, "users.manage");
  const perms = useFetch(() => api.get("/api/v1/permissions"), []);
  if (!isAdmin) return null;  // маршрут и пункт меню доступны только администратору
  return <AdminPanel perms={perms} />;
}

function AdminPanel({ perms }: { perms: any }) {
  const me = useStore((s) => s.user);
  const roles = useFetch(() => api.get("/api/v1/admin/roles"), []);
  const users = useFetch(() => api.get("/api/v1/admin/users"), []);
  const [roleDlg, setRoleDlg] = useState<any>(null);
  const [userDlg, setUserDlg] = useState(false);
  const [pwdFor, setPwdFor] = useState<any>(null);
  const reload = () => { roles.reload(); users.reload(); perms.reload(); };
  const activeRoles = (roles.data ?? []).filter((r: any) => r.active);
  const update = async (u: any, patch: any, msg: string) => {
    try { await api.put(`/api/v1/admin/users/${u.id}`, patch); useStore.getState().toast("ok", msg); users.reload(); roles.reload(); }
    catch (e) { notifyError(e); }
  };
  return (
    <div className="page">
      <h1>Пользователи и роли</h1>
      <p className="muted" style={{ margin: 0 }}>Системные роли и их права заданы в коде и не меняются. Пользовательской роли можно выдать любые права,
        кроме «Управление пользователями и ролями» — оно есть только у администратора. Все изменения записываются в аудит.</p>

      <section className="panel">
        <div className="panel-h"><h2 className="grow">Роли</h2>
          <ActionButton kind="primary" onClick={() => setRoleDlg({ mode: "create" })}>＋ Добавить роль</ActionButton></div>
        {roles.loading && !roles.data ? <Loading /> : roles.error ? <ErrorBox error={roles.error} retry={roles.reload} /> : (
          <table className="t">
            <thead><tr><th>Роль</th><th>Тип</th><th>Права</th><th>Пользователей</th><th>Действия</th></tr></thead>
            <tbody>{roles.data.map((r: any) => (
              <tr key={r.id}>
                <td><b>{r.name}</b><div className="faint mono">{r.id}</div>{r.description && <div className="muted">{r.description}</div>}</td>
                <td>{r.system ? <Badge cls="info">системная</Badge> : <Badge cls={r.active ? "ok" : "muted"}>{r.active ? "пользовательская" : "отключена"}</Badge>}</td>
                <td style={{ maxWidth: 420 }}>{r.permissions.map((p: string) => <span key={p} className="badge muted" style={{ margin: 2 }}>{title(perms.data, p)}</span>)}</td>
                <td>{r.users}</td>
                <td>{r.system ? <span className="faint">не изменяется</span> : (
                  <div className="row wrap">
                    <ActionButton small onClick={() => setRoleDlg({ mode: "edit", role: r })}>Изменить</ActionButton>
                    <ActionButton small onClick={async () => {
                      try { await api.put(`/api/v1/admin/roles/${r.id}`, { active: !r.active }); useStore.getState().toast("ok", r.active ? `Роль «${r.name}» отключена` : `Роль «${r.name}» включена`); reload(); }
                      catch (e) { notifyError(e); }
                    }}>{r.active ? "Отключить" : "Включить"}</ActionButton>
                    <ActionButton small kind="danger" disabledReason={r.users ? `Роль назначена пользователям (${r.users}) — сначала смените им роль.` : null}
                      onClick={async () => {
                        try { await api.del(`/api/v1/admin/roles/${r.id}`); useStore.getState().toast("ok", `Роль «${r.name}» удалена`); reload(); }
                        catch (e) { notifyError(e); }
                      }}>Удалить</ActionButton>
                  </div>)}</td>
              </tr>))}</tbody>
          </table>)}
      </section>

      <section className="panel">
        <div className="panel-h"><h2 className="grow">Пользователи</h2>
          <ActionButton kind="primary" onClick={() => setUserDlg(true)}>＋ Добавить пользователя</ActionButton></div>
        {users.loading && !users.data ? <Loading /> : users.error ? <ErrorBox error={users.error} retry={users.reload} /> :
          !users.data?.length ? <Empty text="Пользователей нет" /> : (
            <table className="t">
              <thead><tr><th>Логин</th><th>Имя</th><th>Роль</th><th>Станция / ПТО / бригада</th><th>Статус</th><th>Действия</th></tr></thead>
              <tbody>{users.data.map((u: any) => (
                <tr key={u.id}>
                  <td className="mono">{u.username}{u.id === me?.id && <div className="faint">это вы</div>}</td>
                  <td>{u.full_name}</td>
                  <td><select aria-label={`Роль пользователя ${u.username}`} value={u.role}
                    onChange={(e) => update(u, { role: e.target.value }, `${u.username}: роль изменена`)}>
                    {(roles.data ?? []).filter((r: any) => r.active || r.id === u.role).map((r: any) => <option key={r.id} value={r.id}>{r.name}</option>)}
                  </select></td>
                  <td><ScopeEditor u={u} onSave={(sc) => update(u, sc, `${u.username}: привязка изменена`)} /></td>
                  <td>{u.active ? <Badge cls="ok" icon="●">активен</Badge> : <Badge cls="bad" icon="✕">заблокирован</Badge>}</td>
                  <td><div className="row wrap">
                    <ActionButton small kind={u.active ? "danger" : ""} onClick={() => update(u, { active: !u.active }, u.active ? `${u.username} заблокирован` : `${u.username} разблокирован`)}>
                      {u.active ? "Заблокировать" : "Разблокировать"}</ActionButton>
                    <ActionButton small onClick={() => setPwdFor(u)}>Сменить пароль</ActionButton>
                  </div></td>
                </tr>))}</tbody>
            </table>)}
      </section>

      {perms.data && <PermissionMatrix data={perms.data} />}
      {roleDlg && <RoleDialog perms={perms.data} dlg={roleDlg} onClose={() => setRoleDlg(null)} onDone={() => { setRoleDlg(null); reload(); }} />}
      {userDlg && <UserDialog roles={activeRoles} onClose={() => setUserDlg(false)} onDone={() => { setUserDlg(false); reload(); }} />}
      {pwdFor && <PasswordDialog u={pwdFor} onClose={() => setPwdFor(null)} />}
    </div>
  );
}

const title = (perms: any, code: string) => perms?.matrix?.find((m: any) => m.action === code)?.title ?? code;

function RoleDialog({ perms, dlg, onClose, onDone }: { perms: any; dlg: any; onClose: () => void; onDone: () => void }) {
  const edit = dlg.mode === "edit";
  const [id, setId] = useState(edit ? dlg.role.id : "");
  const [name, setName] = useState(edit ? dlg.role.name : "");
  const [desc, setDesc] = useState(edit ? dlg.role.description : "");
  const [sel, setSel] = useState<Set<string>>(new Set(edit ? dlg.role.permissions : ["state.view"]));
  const [err, setErr] = useState<ApiError | null>(null);
  const key = useState(newKey())[0];
  const toggle = (p: string) => setSel((s) => { const n = new Set(s); n.has(p) ? n.delete(p) : n.add(p); return n; });
  const submit = async () => {
    try {
      const body = { name, description: desc, permissions: [...sel] };
      if (edit) await api.put(`/api/v1/admin/roles/${dlg.role.id}`, body);
      else await api.post("/api/v1/admin/roles", { id, ...body }, key);
      useStore.getState().toast("ok", edit ? `Роль «${name}» сохранена` : `Роль «${name}» создана`);
      onDone();
    } catch (e) { setErr(e as ApiError); }
  };
  return (
    <Modal title={edit ? `Изменить роль «${dlg.role.name}»` : "Новая роль"} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Отмена</button><button className="btn primary" disabled={name.trim().length < 3 || (!edit && id.trim().length < 3)} onClick={submit}>{edit ? "Сохранить" : "Создать роль"}</button></>}>
      <div className="form-grid">
        <label className="f">Идентификатор (латиница)<input value={id} disabled={edit} onChange={(e) => setId(e.target.value.toLowerCase())} placeholder="shift_master" /></label>
        <label className="f">Название<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Сменный мастер" /></label>
      </div>
      <label className="f">Описание<input value={desc} onChange={(e) => setDesc(e.target.value)} /></label>
      <fieldset className="col" style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 10 }}>
        <legend>Права роли</legend>
        {perms.matrix.map((m: any) => {
          const reserved = m.reserved, base = m.action === "state.view";
          return (
            <label key={m.action} className="row" title={reserved ? "Только системная роль «Администратор»" : base ? "Базовое право — выдаётся всегда" : ""}>
              <input type="checkbox" checked={base || (sel.has(m.action) && !reserved)} disabled={reserved || base} onChange={() => toggle(m.action)} />
              <span>{m.title}</span><span className="faint mono">{m.action}</span>
              {reserved && <span className="why">только администратор</span>}{base && <span className="why">базовое право</span>}
            </label>);
        })}
      </fieldset>
      {err && <ErrorBox error={err} />}
    </Modal>
  );
}

/** Явная привязка пользователя: станция, ПТО / зона, бригада (области видимости сообщений и заданий). */
function ScopeEditor({ u, onSave }: { u: any; onSave: (sc: any) => void }) {
  const [edit, setEdit] = useState(false);
  const [f, setF] = useState({ station_id: u.scope?.station_id ?? "", pto_id: u.scope?.pto_id ?? "", brigade_id: u.scope?.brigade_id ?? "" });
  if (!edit) return (
    <button className="btn ghost small" onClick={() => setEdit(true)} title="Изменить привязку">
      {u.scope ? `${u.scope.station_id} · ${u.scope.pto_id ?? "—"} · ${u.scope.brigade_id ?? "—"}` : <span className="faint">основная станция (по умолчанию)</span>}
    </button>
  );
  return (
    <div className="row wrap" style={{ gap: 4 }}>
      <input aria-label="Станция" style={{ width: 70 }} value={f.station_id} onChange={(e) => setF({ ...f, station_id: e.target.value })} placeholder="ALM" />
      <input aria-label="ПТО" style={{ width: 80 }} value={f.pto_id} onChange={(e) => setF({ ...f, pto_id: e.target.value })} placeholder="ПТО" />
      <input aria-label="Бригада" style={{ width: 70 }} value={f.brigade_id} onChange={(e) => setF({ ...f, brigade_id: e.target.value })} placeholder="BR-1" />
      <button className="btn small primary" disabled={!f.station_id.trim()} onClick={() => { onSave(f); setEdit(false); }}>✓</button>
      <button className="btn small" onClick={() => setEdit(false)}>✕</button>
    </div>
  );
}

function UserDialog({ roles, onClose, onDone }: { roles: any[]; onClose: () => void; onDone: () => void }) {
  const [f, setF] = useState({ username: "", full_name: "", role: roles.find((r) => r.id === "observer")?.id ?? roles[0]?.id, password: "",
    station_id: "", pto_id: "", brigade_id: "" });
  const [err, setErr] = useState<ApiError | null>(null);
  const key = useState(newKey())[0];
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return (
    <Modal title="Новый пользователь" onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Отмена</button><button className="btn primary" disabled={f.password.length < 8 || f.username.length < 3 || f.full_name.length < 2} onClick={async () => {
        try { await api.post("/api/v1/admin/users", { ...f, station_id: f.station_id || null, pto_id: f.pto_id || null, brigade_id: f.brigade_id || null }, key); useStore.getState().toast("ok", `Пользователь ${f.username} создан`); onDone(); } catch (e) { setErr(e as ApiError); }
      }}>Создать</button></>}>
      <div className="form-grid">
        <label className="f">Логин<input value={f.username} onChange={set("username")} autoComplete="off" /></label>
        <label className="f">Имя (как в журнале)<input value={f.full_name} onChange={set("full_name")} /></label>
        <label className="f">Роль<select value={f.role} onChange={set("role")}>{roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}</select></label>
        <label className="f">Пароль (не менее 8 символов)<input type="password" value={f.password} onChange={set("password")} autoComplete="new-password" /></label>
        <label className="f">Станция (пусто — основная)<input value={f.station_id} onChange={set("station_id")} placeholder="ALM" /></label>
        <label className="f">ПТО / зона<input value={f.pto_id} onChange={set("pto_id")} placeholder="PTO" /></label>
        <label className="f">Бригада<input value={f.brigade_id} onChange={set("brigade_id")} placeholder="BR-1" /></label>
      </div>
      {err && <ErrorBox error={err} />}
      {err?.details?.fields && <ul>{err.details.fields.map((x: any, i: number) => <li key={i}>{x.field}: {x.message}</li>)}</ul>}
    </Modal>
  );
}

function PasswordDialog({ u, onClose }: { u: any; onClose: () => void }) {
  const [p, setP] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  return (
    <Modal title={`Новый пароль: ${u.username}`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Отмена</button><button className="btn primary" disabled={p.length < 8} onClick={async () => {
        try { await api.postPlain(`/api/v1/admin/users/${u.id}/password`, { password: p }); useStore.getState().toast("ok", "Пароль изменён"); onClose(); } catch (e) { setErr(e as ApiError); }
      }}>Сохранить</button></>}>
      <label className="f">Пароль (не менее 8 символов)<input type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="new-password" /></label>
      {err && <ErrorBox error={err} />}
    </Modal>
  );
}

export function PermissionMatrix({ data }: { data: any }) {
  const roles = Object.keys(data.roles);
  return (
    <section className="panel">
      <div className="panel-h"><h2>Матрица прав</h2></div>
      <div className="panel-b" style={{ overflow: "auto" }}>
        <p className="muted" style={{ marginTop: 0 }}>{data.note} Права проверяются на сервере для каждой изменяющей операции.</p>
        <table className="t"><thead><tr><th>Действие</th>{roles.map((r) => <th key={r}>{data.roles[r]}{!data.system_roles?.includes(r) && <div className="faint">пользовательская</div>}</th>)}</tr></thead>
          <tbody>{data.matrix.map((m: any) => <tr key={m.action}><td>{m.title}<div className="faint mono">{m.action}</div></td>
            {roles.map((r) => <td key={r} aria-label={m.roles.includes(r) ? "разрешено" : "запрещено"}>{m.roles.includes(r) ? <span style={{ color: "var(--ok)" }}>✓ да</span> : <span className="faint">— нет</span>}</td>)}</tr>)}</tbody></table>
      </div>
    </section>
  );
}
