// Статусы всегда показываются цветом И текстом/значком (цвет не единственный носитель смысла).

export const TRACK_STATUS: Record<string, { label: string; icon: string; cls: string }> = {
  free: { label: "Свободен", icon: "○", cls: "st-free" },
  occupied: { label: "Занят", icon: "■", cls: "st-occupied" },
  unknown: { label: "Неизвестно", icon: "?", cls: "st-unknown" },
  contradictory: { label: "Противоречие", icon: "!", cls: "st-contra" },
  closed: { label: "Закрыт", icon: "✕", cls: "st-closed" },
  reserved: { label: "Зарезервирован", icon: "◇", cls: "st-reserved" },
};

export const SEVERITY: Record<string, { label: string; icon: string; cls: string }> = {
  critical: { label: "Критично", icon: "⛔", cls: "sev-critical" },
  high: { label: "Высокая", icon: "▲", cls: "sev-high" },
  medium: { label: "Средняя", icon: "●", cls: "sev-medium" },
  low: { label: "Низкая", icon: "·", cls: "sev-low" },
};

export const OP_COLORS: Record<string, string> = {
  arrival: "var(--op-arrival)", departure: "var(--op-departure)", inspection: "var(--op-inspection)",
  shunting: "var(--op-shunting)", unloading: "var(--op-cargo)", loading: "var(--op-cargo)", repair: "var(--op-repair)",
  sorting: "var(--op-sorting)", dwell: "var(--op-dwell)", uncoupling: "var(--op-repair)",
};

export const OP_LABEL: Record<string, string> = {
  arrival: "Прибытие", departure: "Отправление", inspection: "Техосмотр", shunting: "Манёвры", unloading: "Выгрузка",
  loading: "Погрузка", repair: "Ремонт", sorting: "Расформирование", dwell: "Стоянка", uncoupling: "Отцепка вагона",
};

export const DECISION: Record<string, { label: string; cls: string; icon: string }> = {
  available: { label: "Приём возможен", cls: "ok", icon: "✓" },
  available_with_warnings: { label: "Возможен с предупреждениями", cls: "warn", icon: "!" },
  unavailable: { label: "Приём недоступен", cls: "bad", icon: "✕" },
  insufficient_data: { label: "Недостаточно данных", cls: "unknown", icon: "?" },
};

export const ITEM_STATUS: Record<string, { label: string; icon: string; cls: string }> = {
  ok: { label: "Выполнено", icon: "✓", cls: "ok" },
  warning: { label: "Предупреждение", icon: "!", cls: "warn" },
  fail: { label: "Нарушено", icon: "✕", cls: "bad" },
  insufficient_data: { label: "Недостаточно данных", icon: "?", cls: "unknown" },
};

export const DATA_STATE: Record<string, { label: string; cls: string; icon: string }> = {
  actual: { label: "Актуальные", cls: "ok", icon: "✓" },
  stale: { label: "Устаревшие", cls: "warn", icon: "⏱" },
  missing: { label: "Отсутствуют", cls: "unknown", icon: "∅" },
  contradictory: { label: "Противоречивые", cls: "bad", icon: "!" },
  invalid: { label: "Некорректные", cls: "bad", icon: "✕" },
  not_monitored: { label: "Без датчика", cls: "muted", icon: "—" },
};

export const CONN: Record<string, { label: string; cls: string; icon: string }> = {
  online: { label: "На связи", cls: "ok", icon: "●" },
  offline: { label: "Нет связи", cls: "bad", icon: "✕" },
  unknown: { label: "Нет данных", cls: "unknown", icon: "?" },
};

export const DEVICE_KIND: Record<string, string> = {
  track_circuit: "Рельсовая цепь (занятость)", switch_sensor: "Контроль положения стрелки", rfid_reader: "RFID-считыватель",
  loco_gps: "ГНСС-трекер локомотива", cargo_equipment: "Погрузочное оборудование", repair_diag: "Диагностика ремонтной зоны",
};

export const ROLE_LABEL: Record<string, string> = {
  train_dispatcher: "Поездной диспетчер", station_dispatcher: "Станционный диспетчер", duty_officer: "Дежурный по станции",
  admin: "Администратор", observer: "Наблюдатель",
};

export const REQ_STATUS_CLS: Record<string, string> = {
  new: "muted", checked: "info", confirmed: "ok", rejected: "bad", cancelled: "muted", in_transit: "info",
  arrived: "info", completed: "ok", expired: "warn",
};
