// Слой подписей поверх canvas: проекция якорей каждый кадр, раскладка без наложений
// по приоритету (не чаще ~7 раз в секунду) и ограничение числа видимых подписей.
import { useFrame, useThree } from "@react-three/fiber";
import { createContext, useContext, useEffect, useRef } from "react";
import * as THREE from "three";
import { layoutLabels, type LabelCand } from "./geometry";

export interface LabelSpec {
  id: string; text: string; sub?: string; cls?: string; priority: number;
  /** мировая точка якоря (вызывается каждый кадр — для движущихся составов) */
  pos: () => [number, number, number] | null;
  /** видна только ближе этой дистанции камеры до якоря */
  maxDist?: number;
  onClick?: () => void;
}

export class LabelRegistry {
  groups = new Map<string, LabelSpec[]>();
  version = 0;
  replace(group: string, specs: LabelSpec[]) { this.groups.set(group, specs); this.version++; }
  remove(group: string) { this.groups.delete(group); this.version++; }
  all() { const out: LabelSpec[] = []; for (const g of this.groups.values()) out.push(...g); return out; }
}

export const LabelCtx = createContext<LabelRegistry | null>(null);

export function useLabelGroup(group: string, specs: LabelSpec[], deps: any[]) {
  const reg = useContext(LabelCtx);
  useEffect(() => {
    reg?.replace(group, specs);
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => reg?.remove(group), [reg, group]);
}

const MAX_LABELS = 46;
const v = new THREE.Vector3();

/** Компонент внутри Canvas: управляет DOM-подписями в overlay (вне WebGL). */
export function LabelLayer({ overlay, reg }: { overlay: React.RefObject<HTMLDivElement>; reg: LabelRegistry }) {
  const { camera, size } = useThree();
  const pool = useRef(new Map<string, { el: HTMLDivElement; w: number; h: number; text: string }>());
  const visible = useRef(new Set<string>());
  const lastLayout = useRef(0);
  const lastVersion = useRef(-1);
  useEffect(() => () => { for (const p of pool.current.values()) p.el.remove(); pool.current.clear(); }, []);
  useFrame(() => {
    const root = overlay.current;
    if (!root) return;
    const specs = reg.all();
    const now = performance.now();
    const proj = new Map<string, { x: number; y: number; d: number }>();
    for (const s of specs) {
      const p = s.pos();
      if (!p) continue;
      v.set(p[0], p[1], p[2]);
      const d = camera.position.distanceTo(v);
      if (s.maxDist && d > s.maxDist) continue;
      v.project(camera);
      if (v.z > 1 || v.z < -1) continue;
      proj.set(s.id, { x: (v.x + 1) / 2 * size.width, y: (1 - v.y) / 2 * size.height, d });
    }
    if (now - lastLayout.current > 140 || reg.version !== lastVersion.current) {
      lastLayout.current = now; lastVersion.current = reg.version;
      // синхронизация пула элементов
      const ids = new Set(specs.map((s) => s.id));
      for (const [id, p] of pool.current) if (!ids.has(id)) { p.el.remove(); pool.current.delete(id); }
      const cands: LabelCand[] = [];
      for (const s of specs) {
        let p = pool.current.get(s.id);
        const text = s.text + (s.sub ? `\n${s.sub}` : "");
        if (!p) {
          const el = document.createElement("div");
          el.className = `tw-label ${s.cls ?? ""}`;
          el.style.display = "none";
          root.appendChild(el);
          p = { el, w: 0, h: 0, text: "" };
          pool.current.set(s.id, p);
        }
        if (p.text !== text || p.el.className !== `tw-label ${s.cls ?? ""}`) {
          p.el.className = `tw-label ${s.cls ?? ""}`;
          p.el.replaceChildren();
          const b = document.createElement("b"); b.textContent = s.text; p.el.appendChild(b);
          if (s.sub) { const sm = document.createElement("small"); sm.textContent = s.sub; p.el.appendChild(sm); }
          p.el.onclick = s.onClick ? (e) => { e.stopPropagation(); s.onClick!(); } : null;
          p.el.style.pointerEvents = s.onClick ? "auto" : "none";
          p.text = text; p.w = 0;
        }
        const q = proj.get(s.id);
        if (!q) continue;
        const w = p.w || Math.max(s.text.length, s.sub?.length ?? 0) * 6.4 + 14;
        const h = p.h || (s.sub ? 30 : 18);
        // ближние подписи немного важнее дальних при равном приоритете
        // уже видимые подписи держатся (гистерезис): не мигают при небольших сдвигах камеры и поездов
        const stay = visible.current.has(s.id) ? 6 : 0;
        cands.push({ id: s.id, x: q.x, y: q.y, w, h, priority: s.priority - Math.min(5, q.d / 1500) + stay });
      }
      visible.current = layoutLabels(cands, MAX_LABELS, size.width, size.height);
    }
    for (const [id, p] of pool.current) {
      const q = proj.get(id);
      if (q && visible.current.has(id)) {
        if (p.el.style.display === "none") {
          p.el.style.display = "";
          p.w = p.el.offsetWidth; p.h = p.el.offsetHeight;
        }
        p.el.style.transform = `translate3d(${(q.x - p.w / 2).toFixed(1)}px, ${(q.y - p.h - 6).toFixed(1)}px, 0)`;
      } else if (p.el.style.display !== "none") p.el.style.display = "none";
    }
  });
  return null;
}
