import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { fmtHMS } from "../lib/format";
import { useStore } from "../lib/store";
import { ErrorBox, Loading } from "../components/ui";

const SAMPLES = ["Почему нельзя принять этот состав?", "Когда появится ближайшее окно?", "Что произойдёт, если путь 3 закроется на час?",
  "Какие операции создают перегрузку?", "Как уменьшить задержку?"];

export default function Assistant() {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<{ q: string; a?: any; err?: ApiError }[]>([]);
  const [busy, setBusy] = useState(false);
  const sel = useStore((s) => s.selection);
  const nav = useNavigate();
  const ask = async (question: string) => {
    if (!question.trim()) return;
    setBusy(true);
    const ctx: any = {};
    if (sel?.type === "track") ctx.track_id = sel.id;
    try {
      const a = await api.postPlain("/api/v1/assistant/ask", { question, context: ctx });
      setItems((x) => [...x, { q: question, a }]);
    } catch (e) { setItems((x) => [...x, { q: question, err: e as ApiError }]); }
    finally { setBusy(false); setQ(""); }
  };
  return (
    <div className="page" style={{ maxWidth: 1000 }}>
      <h1>Помощник диспетчера</h1>
      <p className="muted" style={{ margin: 0 }}>Помощник не принимает решений: расчёты выполняют серверные правила и планировщик, помощник объясняет их результат.
        Объяснения строятся по шаблонам; внешняя языковая модель в этом стенде не используется. При нехватке данных помощник называет, чего не хватает.</p>
      <div className="row wrap">{SAMPLES.map((s) => <button key={s} className="btn small" onClick={() => ask(s)} disabled={busy}>{s}</button>)}</div>
      <section className="panel panel-b col" aria-live="polite" style={{ minHeight: 200 }}>
        {!items.length && <span className="muted">Задайте вопрос или выберите пример выше.</span>}
        {items.map((it, i) => (
          <div key={i} className="col">
            <div className="chat-q">{it.q}</div>
            {it.err && <ErrorBox error={it.err} />}
            {it.a && <div className="chat-a col">
              <div>{it.a.text}</div>
              {it.a.missing_data?.length > 0 && <div className="callout unknown"><b>Не хватает данных:</b><ul>{it.a.missing_data.map((m: string, j: number) => <li key={j}>{m}</li>)}</ul></div>}
              {it.a.facts?.length > 0 && <details open={it.a.facts.length <= 6}><summary>Расчётные факты ({it.a.facts.length})</summary><ul>{it.a.facts.map((f: string, j: number) => <li key={j}>{f}</li>)}</ul></details>}
              {it.a.actions?.length > 0 && <div className="row wrap">{it.a.actions.map((a: any, j: number) =>
                <button key={j} className="btn small" onClick={() => nav(a.type === "open_plan" ? "/plan" : "/requests")}>{a.label}</button>)}</div>}
              <div className="faint">Источник: {it.a.source} · расчёт на {fmtHMS(it.a.computed_at)} модельного времени · версия состояния {it.a.based_on_version}</div>
            </div>}
          </div>
        ))}
        {busy && <Loading text="Расчёт…" />}
      </section>
      <form className="row" onSubmit={(e) => { e.preventDefault(); ask(q); }}>
        <input className="grow" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Например: что произойдёт, если путь 4 закроется на 40 мин?" aria-label="Вопрос помощнику" />
        <button className="btn primary" type="submit" disabled={busy || !q.trim()}>Спросить</button>
      </form>
    </div>
  );
}
