// Оценка текущего модельного времени на клиенте для плавной анимации.
//
// Раньше движение досчитывалось от момента ПОЛУЧЕНИЯ сообщения: задержка сети каждый раз разная
// (поезд дёргался вперёд-назад раз в секунду), а пинги и внеочередные обновления без изменения
// модельного времени сбрасывали отсчёт (поезд отскакивал назад). Теперь:
//  * смещение часов сервера оценивается по минимальной задержке (максимум измерений с медленным
//    затуханием) — случайные задержки сети не влияют;
//  * якорь — момент сервера, когда изменилось модельное время; модельное «сейчас» считается от него.
let offsetMs: number | null = null;   // серверное время − клиентское (оценка)
let anchor: { model: number; server: number } | null = null;
let speed = 1;
let running = false;

export function observeServer(serverTimeS: number | undefined, modelTimeIso: string | undefined, spd?: number, run?: boolean) {
  if (!serverTimeS) return;
  const meas = serverTimeS * 1000 - Date.now();
  if (offsetMs === null || meas > offsetMs) offsetMs = meas;   // меньшая задержка → более точная оценка
  else offsetMs += (meas - offsetMs) * 0.02;                     // медленная подстройка к уходу часов
  if (modelTimeIso) {
    const m = Date.parse(modelTimeIso);
    if (!anchor || anchor.model !== m) anchor = { model: m, server: serverTimeS * 1000 };
  }
  if (spd !== undefined) speed = spd;
  if (run !== undefined) running = run;
}

export const serverNowMs = () => Date.now() + (offsetMs ?? 0);

/** Модельное время «сейчас» (мс) или null, если якоря ещё нет. */
export function modelNowMs(): number | null {
  if (!anchor) return null;
  if (!running) return anchor.model;
  return anchor.model + Math.max(0, serverNowMs() - anchor.server) * speed;
}

export function resetClock() { anchor = null; }
