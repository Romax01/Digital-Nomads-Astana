import { ReactNode, useEffect, useRef, useState } from "react";
import { ApiError } from "../lib/api";
import { useStore } from "../lib/store";

/** Кнопка: если действие недоступно, причина показывается рядом (а не только серым цветом). */
export function ActionButton({ children, onClick, disabledReason, kind = "", busy, small, title, type }: {
  children: ReactNode; onClick?: () => void | Promise<any>; disabledReason?: string | null; kind?: string;
  busy?: boolean; small?: boolean; title?: string; type?: "button" | "submit";
}) {
  const [running, setRunning] = useState(false);
  const disabled = !!disabledReason || busy || running;
  const id = useRef(`why-${Math.random().toString(36).slice(2)}`).current;
  return (
    <span className="btn-wrap">
      <button type={type ?? "button"} className={`btn ${kind} ${small ? "small" : ""}`} disabled={disabled}
        aria-describedby={disabledReason ? id : undefined} title={disabledReason || title}
        onClick={async () => {
          if (!onClick) return;
          setRunning(true);
          try { await onClick(); } finally { setRunning(false); }
        }}>
        {(busy || running) && <span className="spinner" style={{ width: 14, height: 14, borderWidth: 2 }} aria-hidden />}
        {children}
      </button>
      {disabledReason && <span id={id} className="why">{disabledReason}</span>}
    </span>
  );
}

export function Badge({ cls, icon, children }: { cls: string; icon?: string; children: ReactNode }) {
  return <span className={`badge ${cls}`}>{icon && <span aria-hidden>{icon}</span>}{children}</span>;
}

export function Loading({ text = "Загрузка…" }: { text?: string }) {
  return <div className="state-box" role="status"><span className="spinner" aria-hidden />{text}</div>;
}
export function Empty({ text, hint }: { text: string; hint?: string }) {
  return <div className="state-box"><b>{text}</b>{hint && <span>{hint}</span>}</div>;
}
export function ErrorBox({ error, retry }: { error: any; retry?: () => void }) {
  const e = error as ApiError;
  return (
    <div className="callout bad" role="alert">
      <b>{e?.message || "Ошибка"}</b>
      {e?.hint && <div className="muted">{e.hint}</div>}
      {e?.code && <div className="faint mono">Код: {e.code}</div>}
      {retry && <div style={{ marginTop: 6 }}><button className="btn small" onClick={retry}>Повторить</button></div>}
    </div>
  );
}

export function Modal({ title, onClose, children, footer }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    ref.current?.querySelector<HTMLElement>("button, input, select, textarea")?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); prev?.focus(); };
  }, []);
  return (
    <div className="modal-back" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <div className="panel-h"><h2 className="grow">{title}</h2><button className="btn ghost" onClick={onClose} aria-label="Закрыть">✕</button></div>
        <div className="panel-b">{children}</div>
        {footer && <div className="panel-h" style={{ borderTop: "1px solid var(--border)", borderBottom: 0, justifyContent: "flex-end" }}>{footer}</div>}
      </div>
    </div>
  );
}

export function Toasts() {
  const toasts = useStore((s) => s.toasts);
  const dismiss = useStore((s) => s.dismiss);
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} role={t.kind === "error" ? "alert" : "status"}>
          <div className="row between"><b>{t.text}</b><button className="btn ghost small" onClick={() => dismiss(t.id)} aria-label="Скрыть">✕</button></div>
          {t.hint && <div className="muted">{t.hint}</div>}
        </div>
      ))}
    </div>
  );
}

/** Хук загрузки данных REST с состояниями загрузки и ошибки. */
export function useFetch<T>(fn: () => Promise<T>, deps: any[] = [], pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const load = async () => {
    try { const d = await fn(); setData(d); setError(null); } catch (e) { setError(e); } finally { setLoading(false); }
  };
  useEffect(() => {
    setLoading(true); load();
    if (pollMs) { const t = setInterval(load, pollMs); return () => clearInterval(t); }
  }, deps);
  return { data, error, loading, reload: load, setData };
}

export function notifyError(e: any) {
  const err = e as ApiError;
  useStore.getState().toast("error", err?.message || "Ошибка", err?.hint || undefined);
}
