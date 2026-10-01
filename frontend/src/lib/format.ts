// Время отображается в часовом поясе станции (по умолчанию Asia/Almaty, UTC+5).

let TZ = "Asia/Almaty";
export const setTimezone = (tz: string) => { if (tz) TZ = tz; };
export const tzLabel = () => (TZ === "Asia/Almaty" ? "UTC+5, Алматы" : TZ);

const hm = () => new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
const hms = () => new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dfull = () => new Intl.DateTimeFormat("ru-RU", { timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

export const fmtHM = (iso?: string | null) => (iso ? hm().format(new Date(iso)) : "—");
export const fmtHMS = (iso?: string | null) => (iso ? hms().format(new Date(iso)) : "—");
export const fmtFull = (iso?: string | null) => (iso ? dfull().format(new Date(iso)) : "—");

export function plural(n: number, one: string, few: string, many: string) {
  const a = Math.abs(Math.trunc(n));
  if (a % 10 === 1 && a % 100 !== 11) return one;
  if (a % 10 >= 2 && a % 10 <= 4 && !(a % 100 >= 12 && a % 100 <= 14)) return few;
  return many;
}

export function fmtMin(m: number | null | undefined) {
  if (m === null || m === undefined || isNaN(m)) return "—";
  const v = Math.round(m);
  if (Math.abs(v) < 60) return `${v} мин`;
  const h = Math.trunc(Math.abs(v) / 60), r = Math.abs(v) % 60;
  return `${v < 0 ? "−" : ""}${h} ч${r ? ` ${r} мин` : ""}`;
}

export function ago(seconds: number) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 120) return `${s} ${plural(s, "секунду", "секунды", "секунд")} назад`;
  const m = Math.round(s / 60);
  return `${m} ${plural(m, "минуту", "минуты", "минут")} назад`;
}

export const ms = (iso?: string | null) => (iso ? new Date(iso).getTime() : NaN);
