// Кабинет диспетчера: «Сообщения работников» — очередь сообщений о дефектах и заявок на работы,
// решения и назначения, переход к объекту цифрового двойника. Данные — те же REST и права, что
// в мобильном разделе; обновление — по событиям WebSocket без перезагрузки.
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../lib/api";
import { useStore } from "../lib/store";
import { Chip, DefectCard, DEFECT_CLS, URG_CLS, URG_ICON, WO_CLS, WorkCard } from "../mobile/cards";
import { ago, fmtDT, useM } from "../mobile/mlib";
import "../mobile/mobile.css";
import { Icon } from "../components/Brand";

/** Синхронизация словарей и счётчика событий кабинета с мобильными карточками. */
function useBridge() {
  const workSeq = useStore((s) => s.workSeq);
  useEffect(() => { useM.getState().bump(); }, [workSeq]);
  useEffect(() => {
    if (!useM.getState().ctx) api.get("/api/v1/mobile/context").then((c) => useM.getState().setCtx(c)).catch(() => undefined);
  }, []);
  const toast = useM((s) => s.toast);
  useEffect(() => { if (toast) useStore.getState().toast(toast.kind === "error" ? "error" : toast.kind === "warn" ? "warn" : "ok", toast.text); }, [toast]);
}

/** Переход к вагону на цифровом двойнике: выбор поезда и камера «Выбранный объект». */
function ShowOnTwin({ trainId }: { trainId?: string | null }) {
  const nav = useNavigate();
  if (!trainId) return null;
  return (
    <button className="btn small" onClick={() => {
      const s = useStore.getState();
      s.select({ type: "train", id: trainId });
      s.setLevel("station");
      nav("/");
      setTimeout(() => useStore.getState().setCam("selected"), 300);
    }}>Показать на цифровом двойнике</button>
  );
}

export default function Work() {
  useBridge();
  const seq = useM((s) => s.seq);
  const [params, setParams] = useSearchParams();
  const [tab, setTab] = useState<"defects" | "work">(params.get("work") ? "work" : "defects");
  const [status, setStatus] = useState("");
  const [urg, setUrg] = useState("");
  const [wagon, setWagon] = useState("");
  const [data, setData] = useState<any>(null);
  const sel = params.get("report") ? { t: "d", id: params.get("report")! } : params.get("work") ? { t: "w", id: params.get("work")! } : null;
  const open = (t: "report" | "work", id: string) => setParams({ [t]: id });
  useEffect(() => {
    const url = tab === "defects"
      ? `/api/v1/defect-reports?scope=station${status ? `&status=${status}` : "&status=open"}${urg ? `&urgency=${urg}` : ""}${wagon ? `&wagon=${wagon}` : ""}&page_size=100`
      : `/api/v1/work-orders?scope=station${status ? `&status=${status}` : "&status=active"}&page_size=100`;
    api.get(url).then(setData).catch(() => setData({ items: [], total: 0 }));
  }, [tab, status, urg, wagon, seq]);
  const dict = useM((s) => s.ctx?.dictionaries);
  return (
    <div className="page work-page">
      <div className="row between wrap">
        <h1>Сообщения работников</h1>
        <span className="muted">Работники отправляют сообщения с телефонов (раздел <a href="/mobile" target="_blank" rel="noreferrer">/mobile</a>). Решение, приёмка работ и замена — только онлайн по актуальному состоянию.</span>
      </div>
      <div className="work-grid">
        <section className="panel work-list">
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "defects"} onClick={() => { setTab("defects"); setStatus(""); }}>Сообщения о дефектах</button>
            <button role="tab" aria-selected={tab === "work"} onClick={() => { setTab("work"); setStatus(""); }}>Заявки на работы</button>
          </div>
          <div className="row wrap" style={{ padding: 8 }}>
            <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Статус">
              <option value="">{tab === "defects" ? "Открытые" : "Активные"}</option>
              {dict && Object.entries(dict[tab === "defects" ? "defect_status" : "work_status"]).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            {tab === "defects" && <>
              <select value={urg} onChange={(e) => setUrg(e.target.value)} aria-label="Срочность"><option value="">Любая срочность</option>
                {dict && Object.entries(dict.urgency).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
              <input value={wagon} onChange={(e) => setWagon(e.target.value.replace(/\D/g, ""))} placeholder="Вагон" style={{ width: 110 }} />
            </>}
          </div>
          <div className="side-scroll">
            {!data ? <div className="state-box">Загрузка…</div> : data.items.length === 0 ? <div className="state-box">Нет записей</div> :
              tab === "defects" ? data.items.map((d: any) => (
                <div key={d.id} className={`item clickable ${sel?.id === d.id ? "sel" : ""}`} onClick={() => open("report", d.id)} tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && open("report", d.id)}>
                  <div className="title"><span className={URG_CLS[d.urgency]}>{URG_ICON[d.urgency]}</span>№ {d.number} · вагон № {d.wagon_number}
                    <span style={{ marginLeft: "auto" }} className={`badge ${DEFECT_CLS[d.status]}`}>{d.status_label}</span></div>
                  <div className="faint">{d.urgency_label} · {d.category_label} · {d.author?.name} · {ago(d.created_at)}
                    {d.status === "submitted" ? ` · ждёт приёма ${d.waiting_min} мин` : ""}{!d.wagon_linked ? " · ⚠ не привязано" : ""}{d.restriction_active ? " · ⛔ ограничение" : ""}</div>
                </div>
              )) : data.items.map((w: any) => (
                <div key={w.id} className={`item clickable ${sel?.id === w.id ? "sel" : ""}`} onClick={() => open("work", w.id)} tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && open("work", w.id)}>
                  <div className="title">№ {w.number} · {w.kind_label}<span style={{ marginLeft: "auto" }} className={`badge ${WO_CLS[w.status]}`}>{w.status_label}</span></div>
                  <div className="faint">Вагон № {w.wagon?.number} · {w.assignee?.name ?? "не назначен"} · срок {fmtDT(w.due_at)}{w.overdue ? " · ⚠ просрочено" : ""}</div>
                </div>
              ))}
          </div>
        </section>
        <section className="m-embed m-scope work-card">
          {!sel ? <div className="state-box">Выберите сообщение или заявку слева</div> : sel.t === "d"
            ? <DefectCard id={sel.id} onOpenWork={(id) => open("work", id)} desktopExtra={(d) => <ShowOnTwin trainId={d.wagon?.train?.id ?? d.train_id} />} />
            : <WorkCard id={sel.id} onOpenDefect={(id) => open("report", id)} desktopExtra={(w) => <ShowOnTwin trainId={w.wagon?.train?.id} />} />}
        </section>
      </div>
    </div>
  );
}

/** Колокольчик уведомлений в шапке кабинета (уведомления хранятся в БД, обновляются по WS). */
export function NotificationsBell() {
  const workSeq = useStore((s) => s.workSeq);
  const [data, setData] = useState<any>(null);
  const [open, setOpen] = useState(false);
  const nav = useNavigate();
  useEffect(() => { api.get("/api/v1/notifications?limit=20").then(setData).catch(() => undefined); }, [workSeq, open]);
  if (!data) return null;
  return (
    <div className="bell">
      <button className="btn ghost icon-btn" title="Уведомления" aria-label={`Уведомления: ${data.unread} новых`} onClick={() => setOpen(!open)}>
        <Icon name="bell" />{data.unread ? <span className="badge solid bad bell-count">{data.unread}</span> : null}
      </button>
      {open && (
        <div className="bell-pop panel" role="dialog" aria-label="Уведомления">
          <div className="row between" style={{ padding: 8 }}><b>Уведомления</b>
            {data.unread > 0 && <button className="btn small" onClick={() => api.postPlain("/api/v1/notifications/read", { all: true }).then(() => setOpen(false))}>Прочитать все</button>}</div>
          {data.items.length === 0 && <div className="state-box">Нет уведомлений</div>}
          {data.items.map((n: any) => (
            <div key={n.id} className={`item clickable ${n.read ? "" : "unread"}`} onClick={async () => {
              if (!n.read) await api.postPlain("/api/v1/notifications/read", { ids: [n.id] }).catch(() => undefined);
              setOpen(false);
              nav(`/work?${n.entity_type === "defect" ? "report" : "work"}=${n.entity_id}`);
            }}>
              <b>{n.title}</b><span className="faint">{n.body}</span><span className="faint">{ago(n.created_at)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export { Chip };
