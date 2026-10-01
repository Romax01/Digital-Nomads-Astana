import { useEffect, useMemo, useRef, useState } from "react";
import { forecastOccupancy } from "../lib/forecast";
import { headNow, pointAt, polyLen, subPath, toPath } from "../lib/geom";
import { TRACK_STATUS } from "../lib/labels";
import { useStore } from "../lib/store";
import type { Topology, ViewState } from "../lib/types";

const TRAIN_COLOR: Record<string, string> = { freight: "var(--text)", transfer: "var(--op-sorting)", passenger: "var(--op-cargo)" };

function useAnimTick(active: boolean) {
  const [, set] = useState(0);
  useEffect(() => {
    if (!active) return;
    let id = 0, last = 0;
    const loop = (t: number) => { if (t - last > 50) { last = t; set((x) => x + 1); } id = requestAnimationFrame(loop); };
    id = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(id);
  }, [active]);
}

export default function Schematic2D({ topo, st }: { topo: Topology; st: ViewState }) {
  const selection = useStore((s) => s.selection);
  const select = useStore((s) => s.select);
  const mode = useStore((s) => s.mode);
  const forecastAt = useStore((s) => s.forecastAt);
  const lastMsgAt = useStore((s) => s.lastMsgAt);
  const b = topo.bounds;
  // снизу запас под легенду, чтобы она не закрывала нижние пути
  const full = { x: b.min_x, y: b.min_y - 20, w: b.max_x - b.min_x, h: b.max_y - b.min_y + 110 };
  const [vb, setVb] = useState(full);
  const drag = useRef<{ x: number; y: number; vb: typeof vb } | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const fc = useMemo(() => (mode === "forecast" && forecastAt ? forecastOccupancy(st, forecastAt) : null), [mode, forecastAt, st]);

  const zoom = (k: number, cx?: number, cy?: number) => setVb((v) => {
    const nw = Math.min(full.w * 1.5, Math.max(200, v.w * k)), nh = nw * (v.h / v.w);
    const px = cx ?? v.x + v.w / 2, py = cy ?? v.y + v.h / 2;
    return { x: px - (px - v.x) * (nw / v.w), y: py - (py - v.y) * (nh / v.h), w: nw, h: nh };
  });
  const onWheel = (e: React.WheelEvent) => {
    const r = svgRef.current!.getBoundingClientRect();
    const cx = vb.x + ((e.clientX - r.left) / r.width) * vb.w, cy = vb.y + ((e.clientY - r.top) / r.height) * vb.h;
    zoom(e.deltaY > 0 ? 1.15 : 1 / 1.15, cx, cy);
  };
  const sel = (type: any, id: string) => select({ type, id });
  const isSel = (type: string, id: string) => selection?.type === type && selection.id === id;
  const nodeMap = Object.fromEntries(topo.nodes.map((n) => [n.id, n]));
  const west = topo.nodes.find((n) => n.kind === "entry" && n.side === "west");
  const east = topo.nodes.find((n) => n.kind === "entry" && n.side === "east");

  return (
    <div className="scheme-wrap">
      <div className="scheme-tools">
        <button className="btn small" onClick={() => zoom(1 / 1.3)} aria-label="Приблизить">＋</button>
        <button className="btn small" onClick={() => zoom(1.3)} aria-label="Отдалить">－</button>
        <button className="btn small" onClick={() => setVb(full)}>Общий вид</button>
      </div>
      <svg ref={svgRef} className="scheme" viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`} role="img"
        aria-label={`Схема станции ${topo.station.name}. Пути доступны клавишей Tab, выбор — Enter.`}
        onWheel={onWheel}
        onMouseDown={(e) => { drag.current = { x: e.clientX, y: e.clientY, vb }; }}
        onMouseMove={(e) => {
          if (!drag.current) return;
          const r = svgRef.current!.getBoundingClientRect();
          const dx = ((e.clientX - drag.current.x) / r.width) * drag.current.vb.w, dy = ((e.clientY - drag.current.y) / r.height) * drag.current.vb.h;
          if (Math.abs(dx) + Math.abs(dy) > 2) setVb({ ...drag.current.vb, x: drag.current.vb.x - dx, y: drag.current.vb.y - dy });
        }}
        onMouseUp={() => { drag.current = null; }} onMouseLeave={() => { drag.current = null; }}>
        <defs>
          <pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="6" stroke="var(--st-closed)" strokeWidth="2" />
          </pattern>
        </defs>
        {/* зоны */}
        {topo.zones.map((z) => (
          <g key={z.id} onClick={() => sel("zone", z.id)} style={{ cursor: "pointer" }}>
            <rect x={z.x - 70} y={z.y - 9} width={140} height={18} rx={4} fill="var(--panel-2)" stroke={isSel("zone", z.id) ? "var(--focus)" : "var(--border)"} />
            <text x={z.x} y={z.y + 4} fontSize={9.5} textAnchor="middle" fill="var(--muted)">{z.name.length > 30 ? z.name.slice(0, 29) + "…" : z.name}</text>
          </g>
        ))}
        {west && <text x={west.x} y={west.y - 12} fontSize={11} fill="var(--muted)">← Отар (демо)</text>}
        {east && <text x={east.x} y={east.y - 12} fontSize={11} textAnchor="end" fill="var(--muted)">Жетыген (демо) →</text>}
        {/* соединения горловин */}
        {topo.connections.filter((c) => c.kind !== "track").map((c) => (
          <polyline key={c.id} points={c.points.map((p) => p.join(",")).join(" ")} fill="none" stroke="var(--st-free)" strokeWidth={2.2} />
        ))}
        {/* пути */}
        {topo.tracks.map((t) => {
          const ts = st.tracks[t.id];
          let status = ts?.status ?? "unknown";
          let occupantNumber = ts?.occupant_number;
          if (fc) { status = fc[t.id] ? "occupied" : (status === "closed" ? "closed" : "free"); occupantNumber = fc[t.id]?.number; }
          const info = TRACK_STATUS[status] ?? TRACK_STATUS.unknown;
          const color = `var(--${info.cls})`;
          const dash = status === "unknown" ? "9 6" : status === "contradictory" ? "3 4" : undefined;
          const p0 = t.points[0], p1 = t.points[t.points.length - 1];
          const mid = pointAt(t.points, polyLen(t.points) / 2);
          const conflict = (ts?.conflict_ids?.length ?? 0) > 0;
          const label = `${ts?.label ?? t.name}: ${info.label}${occupantNumber ? `, поезд № ${occupantNumber}` : ""}. Данные: ${ts?.data_state_label ?? "—"}${conflict ? ". Есть конфликт" : ""}`;
          return (
            <g key={t.id} className="trk" tabIndex={0} role="button" aria-label={label} aria-pressed={isSel("track", t.id)}
              onClick={() => sel("track", t.id)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); sel("track", t.id); } }}>
              <title>{label}</title>
              <polyline className="hit" points={t.points.map((p) => p.join(",")).join(" ")} fill="none" stroke="transparent" strokeWidth={16} />
              {conflict && <polyline points={t.points.map((p) => p.join(",")).join(" ")} fill="none" stroke="var(--bad)" strokeOpacity={0.35} strokeWidth={12} />}
              {isSel("track", t.id) && <polyline points={t.points.map((p) => p.join(",")).join(" ")} fill="none" stroke="var(--focus)" strokeWidth={10} strokeOpacity={0.6} />}
              {status === "closed" && <rect x={Math.min(p0[0], p1[0])} y={p0[1] - 4} width={Math.abs(p1[0] - p0[0])} height={8} fill="url(#hatch)" />}
              <polyline points={t.points.map((p) => p.join(",")).join(" ")} fill="none" stroke={color} strokeWidth={status === "occupied" ? 4 : 3.2} strokeDasharray={dash} />
              <text x={p0[0] + 6} y={p0[1] - 5} fontSize={10.5} fontWeight={700}>{t.number}</text>
              <text x={p1[0] - 6} y={p1[1] - 5} fontSize={9} textAnchor="end" fill="var(--muted)">{t.useful_length_m ? `${t.useful_length_m} м` : "длина ?"}</text>
              {status !== "free" && status !== "occupied" && (
                <text x={mid[0]} y={mid[1] - 5} fontSize={10} textAnchor="middle" fill={color} fontWeight={700}>{info.icon} {info.label}</text>
              )}
              {conflict && <text x={p1[0] + 8} y={p1[1] + 4} fontSize={12} fill="var(--bad)" aria-hidden>⚠</text>}
            </g>
          );
        })}
        {/* стрелки */}
        {topo.nodes.filter((n) => n.kind === "switch").map((n) => {
          const sw = st.switches[n.id];
          const active = !!sw?.route_op;
          const closed = sw?.closed;
          const conf = (sw?.conflict_ids?.length ?? 0) > 0;
          return (
            <g key={n.id} onClick={() => sel("switch", n.id)} style={{ cursor: "pointer" }}>
              <title>{`${n.name}: ${closed ? "неисправна" : active ? "в маршруте" : "свободна"}${sw?.position ? `, положение: ${sw.position === "normal" ? "плюсовое" : "минусовое"}` : ""}`}</title>
              <circle cx={n.x} cy={n.y} r={isSel("switch", n.id) ? 6 : 4} fill={closed ? "var(--bad)" : active ? "var(--accent)" : "var(--panel)"}
                stroke={conf ? "var(--bad)" : "var(--st-free)"} strokeWidth={1.6} />
            </g>
          );
        })}
        {/* составы — позиция рассчитана backend по модели операций */}
        {mode !== "forecast" && <TrainsLayer st={st} />}
        {mode === "forecast" && fc && Object.entries(fc).map(([tid, o]) => {
          const t = topo.tracks.find((x) => x.id === tid);
          if (!t) return null;
          const m = pointAt(t.points, polyLen(t.points) / 2);
          return <text key={tid} x={m[0]} y={m[1] - 6} fontSize={10} textAnchor="middle" fontWeight={700} fill="var(--st-reserved)">◇ {o.number}</text>;
        })}
        {/* маневровые локомотивы */}
        {Object.values(st.resources).filter((r) => r.pos).map((r) => (
          <g key={r.id} onClick={() => sel("resource", r.id)} style={{ cursor: "pointer" }}>
            <title>{`${r.name}: ${r.status_label}`}</title>
            <rect x={r.pos!.x - 5} y={r.pos!.y - 5} width={10} height={10} transform={`rotate(45 ${r.pos!.x} ${r.pos!.y})`}
              fill={r.status === "faulty" ? "var(--bad)" : "var(--op-shunting)"} stroke={isSel("resource", r.id) ? "var(--focus)" : "var(--bg)"} strokeWidth={1.5} />
          </g>
        ))}
        {/* узлы входа */}
        {topo.nodes.filter((n) => n.kind === "entry").map((n) => <circle key={n.id} cx={n.x} cy={n.y} r={5} fill="var(--panel-3)" stroke="var(--muted)" />)}
        {nodeMap && null}
      </svg>
      <div className="legend" aria-label="Легенда">
        {(["free", "occupied", "unknown", "contradictory", "closed"] as const).map((k) => (
          <span key={k} className={TRACK_STATUS[k].cls}><b aria-hidden>{TRACK_STATUS[k].icon}</b> {TRACK_STATUS[k].label}</span>
        ))}
        <span style={{ color: "var(--bad)" }}>⚠ Конфликт</span>
        <span style={{ color: "var(--op-shunting)" }}>◆ Маневровый локомотив</span>
      </div>
    </div>
  );
}


/** Слой составов: перерисовывается для плавной анимации (до 20 кадров/с), остальная схема — нет. */
function TrainsLayer({ st }: { st: ViewState }) {
  const selection = useStore((s) => s.selection);
  const select = useStore((s) => s.select);
  const mode = useStore((s) => s.mode);
  const lastMsgAt = useStore((s) => s.lastMsgAt);
  const moving = mode === "live" && st.meta.running && Object.values(st.trains).some((t) => t.pos?.moving);
  useAnimTick(moving);
  const elapsed = mode === "live" && st.meta.running ? ((Date.now() - lastMsgAt) / 1000) * st.meta.speed : 0;
  const sel = (id: string) => select({ type: "train", id });
  return (
    <g>
      {Object.values(st.trains).map((t) => {
        if (!t.pos || t.pos.path.length < 2) return null;
        const head = headNow(t.pos, elapsed);
        const seg = subPath(t.pos.path, head - t.pos.body, head);
        if (seg.length < 2) return null;
        const hp = pointAt(t.pos.path, head);
        const selected = selection?.type === "train" && selection.id === t.id;
        const lbl = `Поезд № ${t.number}, ${t.status_label}${t.delay_min ? `, задержка ${t.delay_min} мин` : ""}`;
        const d = toPath(seg);
        return (
          <g key={t.id} style={{ cursor: "pointer" }} tabIndex={0} role="button" aria-label={lbl}
            onClick={(e) => { e.stopPropagation(); sel(t.id); }}
            onKeyDown={(e) => { if (e.key === "Enter") sel(t.id); }}>
            <title>{lbl}{t.waiting_reason ? `. Ожидает: ${t.waiting_reason}` : ""}</title>
            {selected && <path d={d} stroke="var(--focus)" strokeWidth={14} strokeOpacity={0.55} fill="none" />}
            <path d={d} stroke="var(--bg)" strokeWidth={10} fill="none" strokeLinecap="round" />
            <path d={d} stroke={TRAIN_COLOR[t.kind] ?? "var(--text)"} strokeWidth={7} fill="none" strokeDasharray="10 1.6" />
            <text x={hp[0]} y={hp[1] - 9} fontSize={10} textAnchor="middle" fontWeight={700}>
              {t.number}{t.delay_min >= 5 ? ` +${t.delay_min}′` : ""}{t.faulty_wagons.length ? " ⚠" : ""}{t.pos.waiting ? " ⏸" : ""}
            </text>
          </g>
        );
      })}
    </g>
  );
}
