// Главный экран — 3D-цифровой двойник (без переключателя 2D/3D).
// Сверху — уровень, поиск и режимы камеры; в центре — сцена; справа — карточка, конфликты,
// рекомендации и датчики; снизу — временная шкала с раскрытием полной диаграммы Ганта.
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import Gantt from "../components/Gantt";
import ObjectCard from "../components/ObjectCard";
import { AlertsList, ConflictsList, RecommendationsList } from "../components/Panels";
import { Empty, Loading } from "../components/ui";
import { TRACK_STATUS } from "../lib/labels";
import { useStore, useViewState } from "../lib/store";
import { hasWebGL } from "../lib/webgl";
import { openStation } from "../twin/actions";

const TwinScene = lazy(() => import("../twin/TwinScene"));

function SearchBox() {
  const st = useViewState();
  const net = useStore((s) => s.network);
  const topo = useStore((s) => s.topology);
  const select = useStore((s) => s.select);
  const setCam = useStore((s) => s.setCam);
  const toast = useStore((s) => s.toast);
  const [q, setQ] = useState("");
  const options = useMemo(() => {
    const out: { label: string; run: () => void }[] = [];
    for (const s of net?.stations ?? []) out.push({ label: `Станция ${s.name}`, run: () => { select({ type: "station", id: s.id }); if (s.is_main) openStation(s.id); else { useStore.getState().setLevel("network"); setTimeout(() => setCam("selected"), 0); } } });
    for (const s of net?.sections ?? []) out.push({ label: `Перегон ${s.name}`, run: () => { select({ type: "section", id: s.id }); useStore.getState().setLevel("network"); setTimeout(() => setCam("selected"), 0); } });
    for (const t of Object.values(st?.trains ?? {})) out.push({ label: `Поезд № ${t.number}`, run: () => {
      select({ type: "train", id: t.id });
      const ph = st?.network?.trains[t.id];
      const onNet = ph && ph.phase !== "at_station";
      useStore.getState().setLevel(onNet ? "network" : "station");
      setTimeout(() => setCam("selected"), 0);
    } });
    for (const t of topo?.tracks ?? []) out.push({ label: `${t.name} (${topo!.station.name})`, run: () => { select({ type: "track", id: t.id }); useStore.getState().setLevel("station"); setTimeout(() => setCam("selected"), 0); } });
    return out;
  }, [net, topo, st?.trains, st?.network]);
  const go = (text: string) => {
    const v = text.trim().toLowerCase();
    if (!v) return;
    const hit = options.find((o) => o.label.toLowerCase() === v) ?? options.find((o) => o.label.toLowerCase().includes(v));
    if (hit) { hit.run(); setQ(hit.label); } else toast("info", `Не найдено: «${text}»`, "Введите название станции, перегона, номер поезда или пути.");
  };
  return (
    <form className="tw-search" role="search" onSubmit={(e) => { e.preventDefault(); go(q); }}>
      <input list="tw-search-list" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Поиск: станция, поезд, путь…" aria-label="Поиск объекта" />
      <datalist id="tw-search-list">{options.slice(0, 300).map((o) => <option key={o.label} value={o.label} />)}</datalist>
      <button className="btn small" type="submit">Найти</button>
    </form>
  );
}

function NoWebGL() {
  const st = useViewState();
  return (
    <div className="tw-nowebgl">
      <div className="callout warn" role="alert">
        <b>3D-двойник недоступен: браузер не поддерживает WebGL или он отключён.</b>
        <div>Все данные и действия доступны в табличных разделах:</div>
        <div className="row wrap" style={{ marginTop: 6 }}>
          <NavLink className="btn small" to="/schedule">Расписание</NavLink>
          <NavLink className="btn small" to="/plan">План и Гант</NavLink>
          <NavLink className="btn small" to="/requests">Заявки</NavLink>
          <NavLink className="btn small" to="/devices">Датчики и связь</NavLink>
          <NavLink className="btn small" to="/load">Загрузка и индекс</NavLink>
        </div>
      </div>
      {st && (
        <table className="t" aria-label="Состояние путей">
          <thead><tr><th>Путь</th><th>Состояние</th><th>Поезд</th><th>Данные</th></tr></thead>
          <tbody>{Object.values(st.tracks).map((t) => (
            <tr key={t.id}><td>{t.label}</td><td>{TRACK_STATUS[t.status].icon} {t.status_label}</td><td>{t.occupant_number ? `№ ${t.occupant_number}` : "—"}</td><td>{t.data_state_label}</td></tr>
          ))}</tbody>
        </table>
      )}
    </div>
  );
}

export default function Overview() {
  const st = useViewState();
  const topo = useStore((s) => s.topology);
  const net = useStore((s) => s.network);
  const netError = useStore((s) => s.networkError);
  const mode = useStore((s) => s.mode);
  const selection = useStore((s) => s.selection);
  const level = useStore((s) => s.level);
  const cam = useStore((s) => s.cam);
  const setCam = useStore((s) => s.setCam);
  const panels = useStore((s) => s.panels);
  const setPanels = useStore((s) => s.setPanels);
  const [tab, setTab] = useState<"card" | "conflicts" | "recs" | "iot">("conflicts");
  const [ganttBy, setGanttBy] = useState<"tracks" | "resources">("tracks");
  const root = useRef<HTMLDivElement>(null);
  const nav = useNavigate();
  const webgl = hasWebGL();
  useEffect(() => { if (selection) setTab("card"); }, [selection]);
  useEffect(() => {
    const h = () => setPanels({ fullscreen: document.fullscreenElement === root.current });
    document.addEventListener("fullscreenchange", h);
    return () => document.removeEventListener("fullscreenchange", h);
  }, []);
  if (!st) return <Loading text={mode === "history" ? "Загрузка истории…" : "Получение состояния станции…"} />;
  const nConf = Object.keys(st.conflicts).length;
  const nRecs = Object.values(st.recommendations).filter((r) => r.status === "active").length;
  const nAlerts = Object.keys(st.alerts).length;
  const k = st.kpi;
  const selTrain = selection?.type === "train";
  const fullscreen = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => undefined);
    else root.current?.requestFullscreen?.().catch(() => useStore.getState().toast("warn", "Полноэкранный режим недоступен в этом браузере"));
  };
  const stationName = topo?.station.name ?? st.meta.station_name;
  return (
    <div ref={root} className={`twin-page ${panels.right ? "" : "no-right"} ${panels.bottom ? "" : "no-bottom"} ${panels.ganttFull ? "gantt-full" : ""} ${panels.fullscreen ? "is-fs" : ""}`}>
      <div className="tw-topbar">
        <nav className="tw-crumbs" aria-label="Уровень просмотра">
          <button className={level === "network" ? "on" : ""} onClick={() => setCam("network")} title="Железнодорожная сеть: станции и перегоны">Сеть</button>
          <span aria-hidden>›</span>
          <button className={level === "station" ? "on" : ""} onClick={() => setCam("station")} title="Подробная модель основной станции">{stationName}</button>
          {level === "station" && <button className="btn small" onClick={() => setCam("network")}>← Вернуться к сети</button>}
        </nav>
        <div className="seg" role="group" aria-label="Камера">
          <button aria-pressed={cam === "network"} onClick={() => setCam("network")}>Сеть</button>
          <button aria-pressed={cam === "station"} onClick={() => setCam("station")}>Общий вид станции</button>
          <button aria-pressed={cam === "selected"} disabled={!selection} title={selection ? "Показать выбранный объект" : "Сначала выберите объект на сцене"} onClick={() => setCam("selected")}>Выбранный объект</button>
          <button aria-pressed={cam === "follow"} disabled={!selTrain} title={selTrain ? (cam === "follow" ? "Нажмите, чтобы выключить слежение" : "Камера будет следовать за выбранным поездом") : "Сначала выберите поезд"}
            onClick={() => setCam(cam === "follow" ? "free" : "follow")}>{cam === "follow" ? "◉ Следую за поездом" : "Следовать за поездом"}</button>
        </div>
        <SearchBox />
        <div className="tw-kpis">
          <button className="tw-kpi" onClick={() => nav("/load")} title="Индекс эффективности (подробно — «Загрузка и индекс»)">Индекс <b>{st.index?.value ?? "—"}</b></button>
          <button className={`tw-kpi ${nConf ? "bad" : "ok"}`} onClick={() => { setTab("conflicts"); setPanels({ right: true }); }}>{nConf ? `⚠ Конфликты ${nConf}` : "✓ Конфликтов нет"}</button>
          <span className="tw-kpi">Задержки ≥5′ <b>{k.delayed_trains}</b></span>
          <span className="tw-kpi">Свободно А <b>{k.free_rd_tracks}/{k.rd_tracks}</b></span>
          <button className={`tw-kpi ${nAlerts ? "unknown" : "ok"}`} onClick={() => { setTab("iot"); setPanels({ right: true }); }}>{nAlerts ? `? Датчики ${nAlerts}` : "✓ Датчики"}</button>
        </div>
        <div className="row" style={{ gap: 4, marginLeft: "auto" }}>
          <button className="btn small" aria-pressed={panels.right} onClick={() => setPanels({ right: !panels.right })} title="Показать/скрыть правую панель">▤ Панель</button>
          <button className="btn small" aria-pressed={panels.bottom} onClick={() => setPanels({ bottom: !panels.bottom })} title="Показать/скрыть временную шкалу">▭ Шкала</button>
          <button className="btn small" onClick={fullscreen} title="Полноэкранный режим (Esc — выход)">{panels.fullscreen ? "⤡ Выйти" : "⤢ Во весь экран"}</button>
        </div>
      </div>
      <section className="tw-main" aria-label="3D-цифровой двойник">
        {!webgl ? <NoWebGL /> : (
          <Suspense fallback={<Loading text="Загрузка 3D…" />}>
            {level === "station" && !topo ? <Loading text="Загрузка схемы станции…" /> : <TwinScene topo={topo} st={st} net={net} netError={netError} />}
          </Suspense>
        )}
      </section>
      {panels.right && (
        <aside className="tw-right panel" aria-label="Подробности">
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "card"} onClick={() => setTab("card")}>Карточка</button>
            <button role="tab" aria-selected={tab === "conflicts"} onClick={() => setTab("conflicts")}>Конфликты {nConf ? `(${nConf})` : ""}</button>
            <button role="tab" aria-selected={tab === "recs"} onClick={() => setTab("recs")}>Рекомендации {nRecs ? `(${nRecs})` : ""}</button>
            <button role="tab" aria-selected={tab === "iot"} onClick={() => setTab("iot")}>Датчики {nAlerts ? `(${nAlerts})` : ""}</button>
            <button className="tab-close" onClick={() => setPanels({ right: false })} aria-label="Свернуть панель" title="Свернуть панель">»</button>
          </div>
          <div className="side-scroll" role="tabpanel">
            {tab === "card" && <div className="panel-b"><ObjectCard st={st} /></div>}
            {tab === "conflicts" && <ConflictsList st={st} />}
            {tab === "recs" && <RecommendationsList st={st} />}
            {tab === "iot" && <AlertsList st={st} />}
          </div>
        </aside>
      )}
      {panels.bottom && (
        <section className="tw-bottom panel" aria-label="Временная шкала операций">
          <div className="panel-h">
            <h3 className="grow">Временная шкала операций {mode === "forecast" ? "· прогноз" : mode === "history" ? "· история" : ""}</h3>
            <div className="seg" role="group" aria-label="Строки диаграммы">
              <button aria-pressed={ganttBy === "tracks"} onClick={() => setGanttBy("tracks")}>По путям</button>
              <button aria-pressed={ganttBy === "resources"} onClick={() => setGanttBy("resources")}>По ресурсам</button>
            </div>
            <button className="btn small" aria-pressed={panels.ganttFull} onClick={() => setPanels({ ganttFull: !panels.ganttFull })}>{panels.ganttFull ? "Свернуть Гант" : "Развернуть Гант"}</button>
            <button className="btn small" onClick={() => nav("/plan")}>Раздел «План и Гант»</button>
            <button className="btn small" onClick={() => setPanels({ bottom: false })} aria-label="Скрыть шкалу">✕</button>
          </div>
          <div className="side-scroll" style={{ padding: "0 8px" }}>
            {Object.keys(st.operations).length
              ? <Gantt st={st} by={ganttBy} compact={!panels.ganttFull} hoursBefore={panels.ganttFull ? 2 : 1} hoursAfter={panels.ganttFull ? 10 : 5} rowH={panels.ganttFull ? 22 : 16} />
              : <Empty text="Нет операций" />}
          </div>
        </section>
      )}
    </div>
  );
}
