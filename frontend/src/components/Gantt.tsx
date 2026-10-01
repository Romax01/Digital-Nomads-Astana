import { useEffect, useMemo, useRef, useState } from "react";
import { fmtHM, ms } from "../lib/format";
import { OP_COLORS, OP_LABEL } from "../lib/labels";
import { useStore } from "../lib/store";
import type { OperationState, ViewState } from "../lib/types";

const KIND_ORDER: Record<string, number> = { main: 0, receiving_departure: 1, sorting: 2, cargo: 3, repair: 4 };

export default function Gantt({ st, by = "tracks", hoursBefore = 1, hoursAfter = 6, rowH = 22, compact = false, override }: {
  st: ViewState; by?: "tracks" | "resources"; hoursBefore?: number; hoursAfter?: number; rowH?: number; compact?: boolean;
  override?: Record<string, { start: string; end: string; track_id: string; resource_ids?: string[] }>;
}) {
  const select = useStore((s) => s.select);
  const selection = useStore((s) => s.selection);
  const wrap = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(900);
  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver((e) => setWidth(Math.max(500, e[0].contentRect.width)));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);
  const now = ms(st.meta.model_time);
  const t0 = now - hoursBefore * 3600e3, t1 = now + hoursAfter * 3600e3;
  const labelW = compact ? 92 : 170;
  const px = (width - labelW - 10) / (t1 - t0);
  const X = (t: number) => labelW + (Math.min(Math.max(t, t0), t1) - t0) * px;

  const rows = useMemo(() => {
    if (by === "tracks") {
      return Object.values(st.tracks).sort((a, b) => (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9) || Number(a.number) - Number(b.number) || a.number.localeCompare(b.number))
        .map((t) => ({ id: t.id, label: t.label, sub: t.status_label, cls: t.status }));
    }
    return Object.values(st.resources).sort((a, b) => a.kind.localeCompare(b.kind) || a.id.localeCompare(b.id))
      .map((r) => ({ id: r.id, label: r.name, sub: r.status_label, cls: r.status }));
  }, [st.tracks, st.resources, by]);
  const rowIdx = Object.fromEntries(rows.map((r, i) => [r.id, i]));
  const bars: { op: OperationState; row: number; s: number; e: number; ps: number; pe: number }[] = [];
  for (const o of Object.values(st.operations)) {
    const ov = override?.[o.id];
    const s = ms(ov?.start ?? o.forecast_start), e = ms(ov?.end ?? o.forecast_end);
    if (e < t0 || s > t1) continue;
    const keys = by === "tracks" ? [ov?.track_id ?? (o.kind === "uncoupling" ? o.from_track_id : o.track_id)] : (ov?.resource_ids ?? o.resource_ids);
    for (const k of keys) {
      if (k && rowIdx[k] !== undefined) bars.push({ op: o, row: rowIdx[k], s, e, ps: ms(o.planned_start), pe: ms(o.planned_end) });
    }
  }
  const closures: { row: number; s: number; e: number; title: string }[] = [];
  if (by === "tracks") {
    for (const inc of Object.values(st.incidents)) {
      if (inc.kind === "track_closure" && inc.status === "active" && rowIdx[inc.object_id] !== undefined)
        closures.push({ row: rowIdx[inc.object_id], s: ms(inc.start), e: inc.end ? ms(inc.end) : t1, title: inc.title });
    }
  }
  for (const mw of (st as any).maintenance ?? []) {
    const k = mw.object_id;
    if ((by === "tracks" && mw.object_type === "track") || (by === "resources" && mw.object_type === "resource"))
      if (rowIdx[k] !== undefined) closures.push({ row: rowIdx[k], s: ms(mw.start), e: ms(mw.end), title: `Окно обслуживания: ${mw.reason}` });
  }
  const H = rows.length * rowH + 26;
  const ticks: number[] = [];
  for (let t = Math.ceil(t0 / 3600e3) * 3600e3; t <= t1; t += (compact ? 3600e3 : 1800e3)) ticks.push(t);
  const selOp = selection?.type === "operation" ? selection.id : null;
  const selTrain = selection?.type === "train" ? selection.id : null;
  return (
    <div className="gantt" ref={wrap} role="figure" aria-label={`Диаграмма Ганта по ${by === "tracks" ? "путям" : "ресурсам"}: ${bars.length} операций`}>
      <svg width={width} height={H}>
        <defs>
          <pattern id="g-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="6" stroke="var(--st-closed)" strokeWidth="2.2" />
          </pattern>
        </defs>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={X(t)} x2={X(t)} y1={18} y2={H} stroke="var(--border)" />
            <text x={X(t) + 3} y={12} fontSize={10.5} fill="var(--muted)">{fmtHM(new Date(t).toISOString())}</text>
          </g>
        ))}
        {rows.map((r, i) => (
          <g key={r.id}>
            <rect x={0} y={22 + i * rowH} width={width} height={rowH} fill={i % 2 ? "var(--panel)" : "var(--panel-2)"} opacity={0.6} />
            <text x={6} y={22 + i * rowH + rowH * 0.68} fontSize={compact ? 10.5 : 11.5} fill="var(--text)"
              style={{ cursor: "pointer" }} onClick={() => select({ type: by === "tracks" ? "track" : "resource", id: r.id })}>
              {r.label.length > (compact ? 13 : 24) ? r.label.slice(0, compact ? 12 : 23) + "…" : r.label}
            </text>
          </g>
        ))}
        {closures.map((c, i) => (
          <rect key={i} x={X(c.s)} y={22 + c.row * rowH + 2} width={Math.max(2, X(c.e) - X(c.s))} height={rowH - 4} fill="url(#g-hatch)" opacity={0.75}>
            <title>{c.title}</title>
          </rect>
        ))}
        {bars.map(({ op, row, s, e, ps, pe }, i) => {
          const y = 22 + row * rowH + 3, h = rowH - 6;
          const x = X(s), w = Math.max(2, X(e) - X(s));
          const conflict = op.conflict_ids.length > 0;
          const selected = selOp === op.id || (selTrain && op.train_id === selTrain);
          const delayed = s - ps > 4 * 60e3 && op.status !== "done";
          const title = `${op.kind_label}${op.train_number ? ` · поезд № ${op.train_number}` : ""} · ${fmtHM(op.forecast_start)}–${fmtHM(op.forecast_end)}` +
            `${delayed ? ` · задержка ${Math.round((s - ps) / 60e3)} мин (план ${fmtHM(op.planned_start)})` : ""}${!op.reserved ? " · не спланирована" : ""}${conflict ? " · конфликт" : ""} · ${op.status_label}`;
          return (
            <g key={`${op.id}-${i}`} style={{ cursor: "pointer" }} tabIndex={compact ? -1 : 0} role="button" aria-label={title}
              onClick={() => select({ type: "operation", id: op.id })}
              onKeyDown={(ev) => { if (ev.key === "Enter") select({ type: "operation", id: op.id }); }}>
              <title>{title}</title>
              {delayed && <rect x={X(ps)} y={y} width={Math.max(2, X(pe) - X(ps))} height={h} fill="none" stroke="var(--muted)" strokeDasharray="3 2" rx={3} />}
              <rect x={x} y={y} width={w} height={h} rx={3} fill={OP_COLORS[op.kind] ?? "var(--op-dwell)"}
                opacity={op.status === "done" ? 0.35 : !op.reserved ? 0.25 : 0.92}
                stroke={selected ? "var(--focus)" : conflict ? "var(--bad)" : op.status === "in_progress" ? "var(--text)" : "none"}
                strokeWidth={selected || conflict ? 2.4 : 1.2} strokeDasharray={!op.reserved ? "4 2" : undefined} />
              {w > 34 && <text x={x + 4} y={y + h * 0.75} fontSize={10} fill="#0b1118" style={{ pointerEvents: "none" }}>
                {conflict ? "⚠ " : ""}{op.train_number ?? OP_LABEL[op.kind]}
              </text>}
            </g>
          );
        })}
        <line x1={X(now)} x2={X(now)} y1={14} y2={H} stroke="var(--focus)" strokeWidth={2} />
        <text x={X(now) + 3} y={H - 4} fontSize={10} fill="var(--focus)">сейчас</text>
      </svg>
      {!compact && (
        <div className="row wrap" style={{ gap: 12, fontSize: 12, padding: "6px 0" }}>
          {Object.entries(OP_LABEL).map(([k, l]) => <span key={k} className="row" style={{ gap: 4 }}><i style={{ width: 12, height: 10, background: OP_COLORS[k], display: "inline-block", borderRadius: 2 }} />{l}</span>)}
          <span>┈ план (при задержке)</span><span style={{ color: "var(--bad)" }}>⚠ конфликт</span><span>▨ закрытие / окно обслуживания</span><span>⬚ не спланировано</span>
        </div>
      )}
    </div>
  );
}
