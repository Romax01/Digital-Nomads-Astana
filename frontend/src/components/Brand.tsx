// Фирменные элементы интерфейса: знак, копирайт «Digital Nomads 2026», набор значков меню и кнопок.
import type { ReactNode } from "react";

/** Знак «Цифровой станции»: два пути и стрелочный перевод. */
export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <defs>
        <linearGradient id="bm-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#3b8ce8" /><stop offset="1" stopColor="#2fd4bd" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="9" fill="url(#bm-g)" />
      <path d="M6 22h20M6 12h9c4 0 6 2 8 6l1.5 4" stroke="#fff" strokeWidth="2.4" fill="none" strokeLinecap="round" />
      <circle cx="24" cy="10" r="2.6" fill="#ffd166" />
    </svg>
  );
}

/** Копирайт: отображается внизу всех экранов (кабинет, вход, мобильный раздел). */
export function Copyright({ compact = false }: { compact?: boolean }) {
  return (
    <div className={`copyright ${compact ? "compact" : ""}`} role="contentinfo" aria-label="© Digital Nomads 2026">
      <span className="copy-sign" aria-hidden="true">©</span>{" "}
      <span className="copy-name">Digital Nomads</span>{" "}
      <span className="copy-year">2026</span>
      {!compact && <>{" "}<span className="copy-note">Цифровая станция · демонстрационный стенд</span></>}
    </div>
  );
}

const P: Record<string, ReactNode> = {
  overview: <><path d="M3 17l9 4 9-4" /><path d="M3 12l9 4 9-4" /><path d="M12 3l9 4-9 4-9-4z" /></>,
  requests: <><path d="M7 4h10l3 3v13H7z" /><path d="M10 11h7M10 15h7M10 7h3" /></>,
  schedule: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7v5l3.5 2" /></>,
  plan: <><path d="M4 6h9M4 12h14M4 18h7" /><path d="M16 4v4M20 10v4M14 16v4" /></>,
  load: <><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>,
  devices: <><path d="M5 12a7 7 0 0114 0" /><path d="M8.5 12a3.5 3.5 0 017 0" /><circle cx="12" cy="12" r="1.2" /><path d="M12 13v7" /></>,
  work: <><path d="M14.5 4.5l5 5-9 9H5.5v-5z" /><path d="M12.5 6.5l5 5" /></>,
  journal: <><path d="M6 3h11a1 1 0 011 1v16a1 1 0 01-1 1H6z" /><path d="M9 7h6M9 11h6M9 15h4" /></>,
  assistant: <><path d="M4 5h16v11H9l-5 4z" /><path d="M8.5 10.5h.01M12 10.5h.01M15.5 10.5h.01" /></>,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1" /></>,
  admin: <><circle cx="9" cy="8" r="3.5" /><path d="M3 20c0-3.5 2.7-6 6-6s6 2.5 6 6" /><path d="M17 8h4M19 6v4" /></>,
  api: <><path d="M8 7l-5 5 5 5M16 7l5 5-5 5M13.5 5l-3 14" /></>,
  network: <><circle cx="5" cy="7" r="2" /><circle cx="19" cy="7" r="2" /><circle cx="12" cy="18" r="2" /><path d="M7 7h10M6 9l5 7.5M18 9l-5 7.5" /></>,
  station: <><path d="M3 17h18M3 12h8c3 0 5 1.5 6.5 5" /><path d="M3 7h18" /></>,
  target: <><circle cx="12" cy="12" r="7" /><circle cx="12" cy="12" r="2.5" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3" /></>,
  follow: <><path d="M4 16l4-4 3 3 6-6" /><path d="M14 9h3v3" /><circle cx="6" cy="19" r="1.5" /></>,
  panel: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M15 4v16" /></>,
  timeline: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 14h18M7 17h5" /></>,
  fullscreen: <><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" /></>,
  exitfs: <><path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5" /></>,
  search: <><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4.5 4.5" /></>,
  back: <><path d="M10 6l-6 6 6 6M4 12h16" /></>,
  sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8" /></>,
  moon: <><path d="M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z" /></>,
  bell: <><path d="M6 16V11a6 6 0 0112 0v5l1.5 2h-15z" /><path d="M10 20.5a2 2 0 004 0" /></>,
  collapse: <><path d="M15 6l-6 6 6 6" /></>,
  expand: <><path d="M9 6l6 6-6 6" /></>,
  logout: <><path d="M15 4h4v16h-4M10 8l-4 4 4 4M6 12h9" /></>,
  mobile: <><rect x="7" y="2.5" width="10" height="19" rx="2" /><path d="M11 18.5h2" /></>,
};

/** Значок интерфейса (stroke-иконки 24×24, цвет — currentColor). */
export function Icon({ name, size = 18, title }: { name: keyof typeof P | string; size?: number; title?: string }) {
  return (
    <svg className="ico" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden={title ? undefined : true} role={title ? "img" : undefined}>
      {title && <title>{title}</title>}
      {P[name]}
    </svg>
  );
}
