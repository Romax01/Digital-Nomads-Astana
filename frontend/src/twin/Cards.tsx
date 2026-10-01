// Карточки объектов 3D-двойника: станция, перегон, датчик, зона. Только просмотр:
// выбор не меняет состояние станции и не требует прав администратора.
import { useNavigate } from "react-router-dom";
import { Badge, Empty, ErrorBox, Loading, useFetch } from "../components/ui";
import { api } from "../lib/api";
import { fmtHM, fmtHMS } from "../lib/format";
import { CONN, DATA_STATE, DEVICE_KIND } from "../lib/labels";
import { useStore } from "../lib/store";
import type { ViewState } from "../lib/types";
import { openStation } from "./actions";

const KIND_RU: Record<string, string> = { freight: "грузовой", transfer: "передаточный", passenger: "пассажирский" };

export function StationCard({ st, id }: { st: ViewState; id: string }) {
  const net = useStore((s) => s.network);
  const level = useStore((s) => s.level);
  const select = useStore((s) => s.select);
  const s = net?.stations.find((q) => q.id === id);
  if (!net) return <Empty text="Модель сети не загружена" hint="Карточка станции появится после загрузки сети." />;
  if (!s) return <Empty text="Станция не найдена в модели сети" />;
  const sum = st.network?.stations[id];
  const trains = Object.values(st.network?.trains ?? {}).filter((p) => p.station_id === id || p.to === id || p.from === id);
  const isMain = s.is_main;
  return (
    <div className="col">
      <div className="row between"><h2 style={{ margin: 0 }}>{s.name}</h2>
        <Badge cls={isMain ? "info" : "muted"}>{isMain ? "Подробная модель" : "Упрощённая модель"}</Badge></div>
      <dl className="kv">
        <dt>Источник данных</dt><dd>{net.source === "demo" ? "демонстрационные (условные) данные" : net.source}</dd>
        <dt>Координаты</dt><dd className="mono">{s.lat.toFixed(4)}° с. ш., {s.lon.toFixed(4)}° в. д.</dd>
        <dt>Длина между горловинами</dt><dd>{(s.half_length_m * 2 / 1000).toFixed(2)} км{isMain ? " (по схеме станции)" : " (условно)"}</dd>
        {isMain ? <>
          <dt>Поездов на станции</dt><dd>{sum?.trains_here ?? "—"}</dd>
          <dt>Свободно путей парка А</dt><dd>{sum?.free_rd_tracks ?? "—"} из {sum?.rd_tracks ?? "—"}</dd>
          <dt>Конфликты в прогнозе</dt><dd className={sum?.conflicts ? "bad" : ""}>{sum?.conflicts ? `⚠ ${sum.conflicts}${sum.critical ? `, критичных ${sum.critical}` : ""}` : "✓ нет"}</dd>
        </> : <>
          <dt>Приёмных путей</dt><dd>{s.simplified?.receiving_tracks ?? "нет данных"}</dd>
          <dt>Допустимая длина поезда</dt><dd>{s.simplified?.max_train_length_m ? `${s.simplified.max_train_length_m} м` : "нет данных"}</dd>
          <dt>Время обработки</dt><dd>{s.simplified?.processing_min ? `${s.simplified.processing_min} мин` : "нет данных"}</dd>
          <dt>Локомотивов</dt><dd>{s.simplified?.locomotives_available ?? "нет данных"}</dd>
          <dt>Принимает</dt><dd>{s.simplified?.accepts?.map((k) => KIND_RU[k] ?? k).join(", ") ?? "нет данных"}</dd>
          <dt>Ограничения</dt><dd className={sum?.restriction ? "warn" : ""}>{sum?.restriction ? `⚠ ${sum.restriction.title}${sum.restriction.until ? ` до ${fmtHM(sum.restriction.until)}` : ""}` : "нет"}</dd>
        </>}
        <dt>На подходе</dt><dd>{sum?.inbound ?? 0}</dd>
      </dl>
      {!isMain && <div className="callout">Для этой станции известны только обобщённые параметры (число приёмных путей, допустимая длина, окна занятости и ограничения). Схема путей не загружена и не изображается.</div>}
      {trains.length > 0 && <>
        <h4>Поезда</h4>
        <div className="row wrap">{trains.slice(0, 12).map((p) => (
          <button key={p.train_id} className="btn small" onClick={() => select({ type: "train", id: p.train_id })}>№ {p.number} · {p.phase_label.toLowerCase()}</button>
        ))}</div>
      </>}
      <div className="row wrap">
        {isMain
          ? <button className="btn small primary" onClick={() => openStation(id)}>{level === "station" ? "Общий вид станции" : "Открыть станцию"}</button>
          : <button className="btn small" disabled title="Подробная модель доступна только для основной станции: схема путей этой станции не загружена.">Открыть станцию</button>}
        {!isMain && <span className="faint">Подробная модель недоступна: схема путей не загружена.</span>}
      </div>
    </div>
  );
}

export function SectionCard({ st, id }: { st: ViewState; id: string }) {
  const net = useStore((s) => s.network);
  const select = useStore((s) => s.select);
  const sec = net?.sections.find((q) => q.id === id);
  if (!sec || !net) return <Empty text="Перегон не найден в модели сети" />;
  const name = (sid: string) => net.stations.find((s) => s.id === sid)?.name ?? sid;
  const trains = Object.values(st.network?.trains ?? {}).filter((p) => p.section_id === id);
  return (
    <div className="col">
      <div className="row between"><h2 style={{ margin: 0 }}>{sec.name}</h2><Badge cls="muted">{sec.tracks_count === 2 ? "Двухпутный" : "Однопутный"}</Badge></div>
      <dl className="kv">
        <dt>Станции</dt><dd>{name(sec.from)} ({sec.from_throat === "west" ? "западная" : "восточная"} горловина) — {name(sec.to)} ({sec.to_throat === "west" ? "западная" : "восточная"} горловина)</dd>
        <dt>Длина</dt><dd>{(sec.length_m / 1000).toFixed(1)} км · {sec.length_source === "data" ? "из данных" : "геодезическая по координатам"}</dd>
        <dt>Пути перегона</dt><dd>{sec.tracks.map((t) => `№ ${t.no}${t.direction === "from_to" ? ` (→ ${name(sec.to)})` : t.direction === "to_from" ? ` (→ ${name(sec.from)})` : " (оба направления)"}`).join("; ")}</dd>
        {sec.physical_spacing_m && <><dt>Междупутье</dt><dd>{sec.physical_spacing_m} м (на схеме сети увеличено для наглядности)</dd></>}
        <dt>Скорость</dt><dd>{sec.max_speed_kmh ? `до ${sec.max_speed_kmh} км/ч` : "нет данных"}</dd>
        <dt>Источник</dt><dd>{sec.source === "demo" ? "демонстрационные данные" : sec.source}</dd>
      </dl>
      <p className="faint">Положение поезда на перегоне — доля пройденного пути по расписанию модели (датчиков на перегоне нет).</p>
      <h4>Поезда на перегоне ({trains.length})</h4>
      {!trains.length ? <p className="muted">Нет.</p> : (
        <table className="t"><thead><tr><th>Поезд</th><th>Направление</th><th>Путь</th><th>Пройдено</th></tr></thead>
          <tbody>{trains.map((p) => (
            <tr key={p.train_id} className="clickable" onClick={() => select({ type: "train", id: p.train_id })}>
              <td>№ {p.number} · {p.wagons} ваг.</td><td>→ {name(p.to ?? "")}</td><td>№ {p.track_no}</td>
              <td>{p.phase === "waiting_entry" ? "ожидает приёма" : `${Math.round((p.frac ?? 0) * 100)} %`}</td>
            </tr>
          ))}</tbody></table>
      )}
    </div>
  );
}

export function DeviceCard({ id }: { id: string }) {
  const nav = useNavigate();
  const topo = useStore((s) => s.topology);
  const { data, error, loading, reload } = useFetch(() => api.get("/api/v1/devices"), [id], 3000);
  if (loading && !data) return <Loading />;
  if (error) return <ErrorBox error={error} retry={reload} />;
  const d = data?.devices?.find((x: any) => x.id === id);
  if (!d) return <Empty text="Устройство не найдено" />;
  const c = CONN[d.connection] ?? CONN.unknown, q = DATA_STATE[d.data_quality] ?? DATA_STATE.missing;
  const obj = topo?.tracks.find((t) => t.id === d.object_id);
  return (
    <div className="col">
      <h2 style={{ margin: 0 }}>{d.name}</h2>
      <dl className="kv">
        <dt>Тип</dt><dd>{DEVICE_KIND[d.kind] ?? d.kind}</dd>
        <dt>Объект</dt><dd>{obj?.name ?? d.object_id}</dd>
        <dt>Связь</dt><dd><Badge cls={c.cls} icon={c.icon}>{c.label}</Badge>{d.heartbeat_age_s != null ? ` heartbeat ${Math.round(d.heartbeat_age_s)} с назад` : ""}</dd>
        <dt>Качество данных</dt><dd><Badge cls={q.cls} icon={q.icon}>{q.label}</Badge>{d.measurement_age_s != null ? ` измерение ${Math.round(d.measurement_age_s)} с назад` : ""}</dd>
        <dt>Порог устаревания</dt><dd>{d.stale_after_s} с</dd>
        <dt>Источник</dt><dd>{d.source_mode === "simulated" ? "симулятор (программное устройство)" : d.source_mode}</dd>
        {d.last_seen_at && <><dt>Последний приём</dt><dd>{fmtHMS(d.last_seen_at)}</dd></>}
      </dl>
      <button className="btn small" onClick={() => nav("/devices")}>Открыть «Датчики и связь»</button>
    </div>
  );
}

export function ZoneCard({ st, id }: { st: ViewState; id: string }) {
  const topo = useStore((s) => s.topology);
  const select = useStore((s) => s.select);
  const z = topo?.zones.find((q) => q.id === id);
  if (!z) return <Empty text="Зона не найдена" />;
  const KIND: Record<string, string> = { platform: "Пассажирская платформа", cargo_front: "Грузовой фронт", repair: "Ремонтное депо", inspection: "Пост осмотра", loco_depot: "Стоянка локомотивов" };
  const code = z.id.split("-Z").pop();
  const res = Object.values(st.resources).filter((r) => r.zone === z.id || r.zone === code);
  return (
    <div className="col">
      <h2 style={{ margin: 0 }}>{z.name}</h2>
      <dl className="kv">
        <dt>Вид</dt><dd>{KIND[z.kind] ?? z.kind}</dd>
        {z.params?.length_m && <><dt>Длина</dt><dd>{z.params.length_m} м</dd></>}
        {z.params?.operations?.length > 0 && <><dt>Операции</dt><dd>{z.params.operations.join(", ")}</dd></>}
      </dl>
      {z.track_ids.length > 0 && <><h4>Пути зоны</h4><div className="row wrap">{z.track_ids.map((t) => (
        <button key={t} className="btn small" onClick={() => select({ type: "track", id: t })}>{st.tracks[t]?.label ?? t} · {st.tracks[t]?.status_label}</button>
      ))}</div></>}
      {res.length > 0 && <><h4>Ресурсы</h4><div className="row wrap">{res.map((r) => (
        <button key={r.id} className="btn small" onClick={() => select({ type: "resource", id: r.id })}>{r.name} · {r.status_label}</button>
      ))}</div></>}
    </div>
  );
}
