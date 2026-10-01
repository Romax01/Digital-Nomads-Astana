import { useState } from "react";
import { api, newKey } from "../lib/api";
import { fmtHM } from "../lib/format";
import { useStore } from "../lib/store";
import { ErrorBox, Modal } from "./ui";

const KIND_TITLES: Record<string, string> = {
  track_closure: "Закрытие пути", switch_failure: "Неисправность стрелки", cargo_delay: "Задержка грузовой операции",
  faulty_wagon: "Неисправный вагон", neighbor_restriction: "Ограничение соседней станции", resource_failure: "Отказ ресурса",
};

export function IncidentDialog({ kind, objectId, objectLabel, onClose }: { kind: string; objectId: string; objectLabel: string; onClose: () => void }) {
  const [duration, setDuration] = useState(kind === "faulty_wagon" ? 0 : 60);
  const [extra, setExtra] = useState(45);
  const [desc, setDesc] = useState("");
  const [err, setErr] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const key = useState(newKey())[0];
  const submit = async () => {
    setBusy(true); setErr(null);
    try {
      await api.post("/api/v1/incidents", { kind, object_id: objectId, duration_min: duration || null,
        extra_min: kind === "cargo_delay" ? extra : null, description: desc }, key);
      useStore.getState().toast("ok", `Инцидент зарегистрирован: ${KIND_TITLES[kind]} — ${objectLabel}`,
        "Конфликты и предложение плана пересчитаются автоматически.");
      onClose();
    } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  return (
    <Modal title={`${KIND_TITLES[kind]}: ${objectLabel}`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Отмена</button><button className="btn primary" disabled={busy} onClick={submit}>Зарегистрировать</button></>}>
      <div className="callout warn">
        Последствия: объект станет недоступен для новых операций на указанный период. Конфликтующие операции будут показаны
        в панели конфликтов, планировщик предложит допустимые варианты. Действие записывается в аудит.
      </div>
      {kind !== "faulty_wagon" && kind !== "cargo_delay" && (
        <label className="f">Длительность, мин (0 — до отмены)<input type="number" min={0} max={1440} value={duration} onChange={(e) => setDuration(+e.target.value)} /></label>
      )}
      {kind === "cargo_delay" && (
        <label className="f">Дополнительная задержка операции, мин<input type="number" min={5} max={600} value={extra} onChange={(e) => setExtra(+e.target.value)} /></label>
      )}
      <label className="f">Описание (необязательно)<textarea rows={2} value={desc} onChange={(e) => setDesc(e.target.value)} /></label>
      {err && <ErrorBox error={err} />}
    </Modal>
  );
}

export function OverrideDialog({ trackId, label, onClose }: { trackId: string; label: string; onClose: () => void }) {
  const [occupied, setOccupied] = useState(true);
  const [reason, setReason] = useState("");
  const [minutes, setMinutes] = useState(15);
  const [err, setErr] = useState<any>(null);
  const key = useState(newKey())[0];
  const submit = async () => {
    try {
      await api.post("/api/v1/observations/override", { object_id: trackId, occupied, reason, valid_minutes: minutes }, key);
      useStore.getState().toast("ok", `Ручное уточнение сохранено: ${label} — ${occupied ? "занят" : "свободен"} на ${minutes} мин`);
      onClose();
    } catch (e) { setErr(e); }
  };
  return (
    <Modal title={`Ручное уточнение: ${label}`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Отмена</button><button className="btn primary" disabled={reason.trim().length < 5} onClick={submit}>Сохранить уточнение</button></>}>
      <div className="callout warn">
        Уточнение заменяет показания датчика на ограниченный срок и разблокирует проверки, зависящие от этого пути.
        Требуются основание и срок действия; запись попадает в аудит. Показания датчика сохраняются.
      </div>
      <div className="seg" role="group" aria-label="Значение">
        <button aria-pressed={occupied} onClick={() => setOccupied(true)}>■ Занят</button>
        <button aria-pressed={!occupied} onClick={() => setOccupied(false)}>○ Свободен</button>
      </div>
      <label className="f">Основание (обязательно)<textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Например: визуальный осмотр составителем, путь свободен" /></label>
      <label className="f">Срок действия, мин<input type="number" min={1} max={120} value={minutes} onChange={(e) => setMinutes(+e.target.value)} /></label>
      {err && <ErrorBox error={err} />}
    </Modal>
  );
}

export function RescheduleOpDialog({ op, tracks, onClose }: { op: any; tracks: any[]; onClose: () => void }) {
  const toLocalInput = (iso: string) => {
    const d = new Date(new Date(iso).getTime() + 5 * 3600e3);  // поле ввода — во времени станции (UTC+5)
    return d.toISOString().slice(0, 16);
  };
  const [start, setStart] = useState(toLocalInput(op.forecast_start));
  const [track, setTrack] = useState(op.track_id);
  const [err, setErr] = useState<any>(null);
  const key = useState(newKey())[0];
  const submit = async () => {
    try {
      const iso = new Date(start + ":00+05:00").toISOString();
      const r = await api.post(`/api/v1/operations/${op.id}/reschedule`, { start: iso, track_id: track !== op.track_id ? track : null,
        reason: "Ручной перенос на диаграмме Ганта" }, key);
      useStore.getState().toast("ok", `Операция перенесена: изменено операций — ${r.changed_operations}`);
      onClose();
    } catch (e) { setErr(e); }
  };
  const sameKind = tracks.filter((t) => t.kind === tracks.find((x) => x.id === op.track_id)?.kind);
  return (
    <Modal title={`Перенести: ${op.kind_label}${op.train_number ? ` поезда № ${op.train_number}` : ""}`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>Отмена</button><button className="btn primary" onClick={submit}>Проверить и перенести</button></>}>
      <p className="muted" style={{ margin: 0 }}>Сейчас: {fmtHM(op.forecast_start)}–{fmtHM(op.forecast_end)}, путь {tracks.find((t) => t.id === op.track_id)?.number}.
        Последующие операции цепочки сдвинутся не раньше окончания предыдущих. Сервер повторно проверит все ограничения
        (пути, маршруты, ресурсы, смены, закрытия, данные датчиков) перед сохранением.</p>
      <label className="f">Новое начало (время станции, UTC+5)<input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></label>
      {["arrival", "inspection", "dwell", "unloading", "loading", "departure", "shunting"].includes(op.kind) && (
        <label className="f">Путь стоянки
          <select value={track} onChange={(e) => setTrack(e.target.value)}>
            {sameKind.map((t) => <option key={t.id} value={t.id}>{t.label} ({t.useful_length_m ?? "?"} м) — {t.status_label}</option>)}
          </select>
        </label>
      )}
      {err && <ErrorBox error={err} />}
      {err?.details?.errors && <ul>{err.details.errors.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul>}
    </Modal>
  );
}
