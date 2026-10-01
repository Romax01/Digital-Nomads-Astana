// Команды навигации 3D-двойника (без зависимости от three.js — используются вне ленивой сцены).
import { useStore } from "../lib/store";

/** Перелёт к станции на уровне «Сеть», затем переход на уровень «Станция». Только просмотр. */
export function openStation(id: string) {
  const s = useStore.getState();
  s.select({ type: "station", id });
  if (s.level === "network") {
    s.setCam("selected");
    setTimeout(() => {
      const n = useStore.getState();
      if (n.level === "network" && n.selection?.type === "station" && n.selection.id === id) n.setCam("station");
    }, 900);
  } else s.setCam("station");
}
