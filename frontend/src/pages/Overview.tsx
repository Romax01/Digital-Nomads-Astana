import { lazy, Suspense, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Gantt from "../components/Gantt";
import ObjectCard from "../components/ObjectCard";
import { AlertsList, ConflictsList, IndexWidget, RecommendationsList } from "../components/Panels";
import Schematic2D from "../components/Schematic2D";
import { hasWebGL } from "../lib/webgl";
import { Empty, Loading } from "../components/ui";
import { useStore, useViewState } from "../lib/store";

const Scene3D = lazy(() => import("../components/Scene3D"));

export default function Overview() {
  const st = useViewState();
  const topo = useStore((s) => s.topology);
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const mode = useStore((s) => s.mode);
  const selection = useStore((s) => s.selection);
  const [tab, setTab] = useState<"card" | "conflicts" | "recs" | "iot">("conflicts");
  const [ganttBy, setGanttBy] = useState<"tracks" | "resources">("tracks");
  const nav = useNavigate();
  const webgl = hasWebGL();
  useEffect(() => { if (selection) setTab("card"); }, [selection]);
  if (!topo || !st) return <Loading text={mode === "history" ? "Загрузка истории…" : "Получение состояния станции…"} />;
  const nConf = Object.keys(st.conflicts).length;
  const nRecs = Object.values(st.recommendations).filter((r) => r.status === "active").length;
  const nAlerts = Object.keys(st.alerts).length;
  const k = st.kpi;
  return (
    <div className="overview">
      <div className="ov-kpis">
        <IndexWidget idx={st.index} onOpen={() => nav("/load")} />
        <div className={`kpi click`} onClick={() => setTab("conflicts")} role="button" tabIndex={0}>
          <small>Конфликты в прогнозе</small><b className={nConf ? "bad" : "ok"} style={{ color: nConf ? "var(--bad)" : "var(--ok)" }}>{nConf ? `⚠ ${nConf}` : "✓ 0"}</b>
          <small>{k.critical ? `из них критичных: ${k.critical}` : "критичных нет"}</small>
        </div>
        <div className="kpi"><small>Поезда с задержкой ≥ 5 мин</small><b>{k.delayed_trains}</b>
          <small>{k.delays[0] ? `макс.: № ${k.delays[0].number} +${k.delays[0].delay_min} мин` : "задержек нет"}</small></div>
        <div className="kpi"><small>Свободно путей парка А</small><b>{k.free_rd_tracks} из {k.rd_tracks}</b><small>на станции поездов: {k.trains_on_station}</small></div>
        <div className="kpi click" onClick={() => setTab("iot")} role="button" tabIndex={0}>
          <small>Проблемы датчиков</small><b style={{ color: nAlerts ? "var(--unknown)" : "var(--ok)" }}>{nAlerts ? `? ${nAlerts}` : "✓ 0"}</b>
          <small>{nAlerts ? "влияют на подтверждение операций" : "данные актуальны"}</small></div>
        <div className="kpi"><small>Активные инциденты</small><b>{Object.values(st.incidents).filter((i) => i.status === "active").length}</b>
          <small>версия состояния {st.meta.state_version}</small></div>
      </div>
      <section className="ov-center" aria-label="Схема станции">
        <div className="row between" style={{ margin: "4px 0" }}>
          <div className="seg" role="group" aria-label="Представление">
            <button aria-pressed={view === "2d"} onClick={() => setView("2d")}>2D-схема</button>
            <button aria-pressed={view === "3d"} onClick={() => setView("3d")} disabled={!webgl} title={webgl ? "" : "WebGL недоступен в этом браузере"}>3D-двойник</button>
          </div>
          {!webgl && <span className="muted">3D недоступен (нет WebGL) — все функции доступны в 2D.</span>}
          <span className="faint">Схема не в масштабе · данные синтетические</span>
        </div>
        {view === "3d" && webgl
          ? <Suspense fallback={<Loading text="Загрузка 3D…" />}><Scene3D topo={topo} st={st} /></Suspense>
          : <Schematic2D topo={topo} st={st} />}
      </section>
      <aside className="ov-side panel" aria-label="Подробности">
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === "card"} onClick={() => setTab("card")}>Карточка</button>
          <button role="tab" aria-selected={tab === "conflicts"} onClick={() => setTab("conflicts")}>Конфликты {nConf ? `(${nConf})` : ""}</button>
          <button role="tab" aria-selected={tab === "recs"} onClick={() => setTab("recs")}>Рекомендации {nRecs ? `(${nRecs})` : ""}</button>
          <button role="tab" aria-selected={tab === "iot"} onClick={() => setTab("iot")}>Датчики {nAlerts ? `(${nAlerts})` : ""}</button>
        </div>
        <div className="side-scroll" role="tabpanel">
          {tab === "card" && <div className="panel-b"><ObjectCard st={st} /></div>}
          {tab === "conflicts" && <ConflictsList st={st} />}
          {tab === "recs" && <RecommendationsList st={st} />}
          {tab === "iot" && <AlertsList st={st} />}
        </div>
      </aside>
      <section className="ov-bottom panel" aria-label="Временная шкала операций">
        <div className="panel-h">
          <h3 className="grow">Временная шкала операций</h3>
          <div className="seg" role="group" aria-label="Строки диаграммы">
            <button aria-pressed={ganttBy === "tracks"} onClick={() => setGanttBy("tracks")}>По путям</button>
            <button aria-pressed={ganttBy === "resources"} onClick={() => setGanttBy("resources")}>По ресурсам</button>
          </div>
          <button className="btn small" onClick={() => nav("/plan")}>Открыть полностью</button>
        </div>
        <div className="side-scroll" style={{ padding: "0 8px" }}>
          {Object.keys(st.operations).length ? <Gantt st={st} by={ganttBy} compact hoursBefore={1} hoursAfter={5} rowH={16} /> : <Empty text="Нет операций" />}
        </div>
      </section>
    </div>
  );
}
