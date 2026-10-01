# Модель данных

Схема — `backend/app/models.py`, миграция — `backend/alembic/versions/0001_initial_schema.py`.

**Единицы:** длины в метрах, длительности в минутах, время — `timestamptz` (в БД UTC, отображение — Asia/Almaty, UTC+5).

**Время:** поля операций, поездов, инцидентов и заявок (`planned_*`, `forecast_*`, `actual_*`, `start_at`, `desired_departure`) хранят **модельное** время симуляции. Служебные поля (`ts`, `created_at`, `received_at`, `observed_at`, `real_time`) хранят **реальное** время. Аудит и доменные события хранят оба (`ts` и `model_time`).

## Топология и станции

| Таблица | Ключевые поля | Примечание |
|---|---|---|
| `stations` | id, name, kind (main/neighbor), is_demo, timezone, config (JSONB) | у основной — полная конфигурация; у соседней — упрощённое состояние (время хода, макс. длина, приёмные пути, `occupancy` интервалами, локомотивы) |
| `parks` | id, station_id, name, kind | receiving_departure, sorting, cargo, repair, main |
| `zones` | id, name, kind, track_ids, x, y, params | cargo_front, repair, inspection, loco_depot, platform; `params.operations` — разрешённые операции |
| `topology_nodes` | id, kind (switch/entry/end), x, y, side | стрелки `W<k>`/`E<k>`, входы, упоры |
| `tracks` | id, number, kind, useful_length_m (м, может быть NULL), allowed_train_kinds, from_node, to_node, zone_id, points | NULL длины ⇒ «Недостаточно данных» |
| `track_connections` | from_node, to_node, kind (track/link/approach), track_id, length_m, points | граф маршрутизации |

## Поезда и операции

| Таблица | Ключевые поля |
|---|---|
| `trains` | number, kind (freight/transfer/passenger), priority 1–5, origin/destination, arrival/departure_side, wagons_count, length_m, loco_length_m, status, scheduled/expected arrival и departure, current_track_id, transfer_request_id |
| `wagons` | number, kind, length_m (м), loaded, condition (ok/faulty/in_repair), position, last_checkpoint, last_seen_at |
| `operations` | train_id (NULL — операция без поезда, напр. ремонт вагона), kind, seq, track_id, from_track_id, side, duration_min, requirements (виды ресурсов), resource_ids, route_nodes, planned/forecast/actual start-end, not_before, status, extra_delay_min, reserved, plan_version, note |

`reserved = false` — требование создано (например, отцепка неисправного вагона), но ещё не размещено планировщиком. Такая операция не может начаться и показывается как конфликт «Требуется планирование».

## Ресурсы и ограничения

| Таблица | Ключевые поля |
|---|---|
| `resources` | kind (shunting_loco, loco_crew, shunting_crew, inspection_team, cargo_equipment, repair_team), home_zone_id, status |
| `resource_shifts` | resource_id, start_at, end_at — интервалы доступности бригад |
| `maintenance_windows` | object_type (track/resource/switch), object_id, start_at, end_at, reason |
| `capacity_rules` | code, name, category, unit, period, source, rule_text, value, policy (hard/soft) |
| `plans` | month, target_wagons, actual_base_wagons, policy (soft / hard_quota) |

## Заявки, резервы, инциденты, планы

| Таблица | Ключевые поля |
|---|---|
| `transfer_requests` | number, from/to_station_id, wagons_count, wagon_kind, train_length_m, priority, split_allowed, desired_departure, status, last_check (JSON результата), last_check_version, train_id, parent_id, decision_reason, row_version |
| `reservations` | resource_key (`track:<id>`, `switch:<id>`, `res:<id>`), start_at, end_at, status (confirmed/released), purpose, train_id, operation_id, request_id |
| `incidents` | kind, object_type, object_id, start_at, end_at (NULL — до отмены), status, params |
| `recommendations` | kind, title, reason, affected, action, effect, computed_at (модельное), computed_real_at, based_on_version, status |
| `plan_versions` | base_state_version, trigger, status, solver, solver_status, solve_ms, summary, changes, assignments, assumptions |

### Ограничение-исключение
```sql
ALTER TABLE reservations ADD CONSTRAINT reservations_no_overlap
EXCLUDE USING gist (resource_key WITH =, tstzrange(start_at, end_at, '[)') WITH &&)
WHERE (status = 'confirmed');
```
Активные резервы одного ресурса не пересекаются: правило действует и для конкурентных транзакций, и для прямых запросов в обход приложения. Интервалы полуоткрытые: резерв до 14:30 не конфликтует с резервом с 14:30. Освобождённые резервы (`released`) сохраняются в истории.

## Статусы и допустимые переходы (`app/domain/statuses.py`)

| Сущность | Переходы |
|---|---|
| Заявка | new → checked / cancelled / expired; checked → checked / confirmed / rejected / cancelled / expired; confirmed → cancelled / in_transit / checked; in_transit → arrived → completed |
| Операция | planned → confirmed / in_progress / cancelled; confirmed → in_progress / cancelled; in_progress → done |
| Поезд | scheduled → approaching / waiting / on_station / cancelled; approaching → on_station / waiting; waiting → on_station; on_station → departed / completed |
| Инцидент | active → resolved |
| Рекомендация | active → applied / stale / dismissed |
| План | proposed → applied / stale / rejected |

Недопустимый переход возвращает `409 INVALID_STATUS_TRANSITION` со списком разрешённых статусов.

## Пользователи и служебные таблицы

| Таблица | Назначение |
|---|---|
| `users` | username, full_name, role, password_hash (PBKDF2-SHA256, 120 000 итераций) |
| `audit_events` | ts, model_time, пользователь и роль, action, entity, summary, before/after, reason |
| `idempotency_keys` | (key, user_id) PK, endpoint, request_hash, response |
| `domain_events` | журнал событий станции |
| `sim_state` | model_time, speed, running, seed, scenario, station_config, plan_state_version, world (неисправности устройств), scheduled_events |

## IoT

| Таблица | Назначение |
|---|---|
| `devices` | реестр: kind, object_id, allowed_event_types, period_s, stale_after_s, status, source_mode, last_seen_at, last_heartbeat_at, last_seq, last_boot_id, health |
| `telemetry_events` | все принятые сообщения; уникальность (device_id, event_id); disposition: applied / late / stale |
| `telemetry_rejects` | отклонённые сообщения с кодом и причиной (к состоянию не применяются) |
| `observations` | последнее наблюдение по (объект, признак, устройство) |
| `manual_overrides` | ручное уточнение: значение, основание, valid_until (реальное время), пользователь |

## Индекс и история

| Таблица | Назначение |
|---|---|
| `index_configs` | версии конфигурации индекса (веса, нормализация, пороги), активна одна |
| `index_snapshots` | значение, категория, составляющие, качество — для динамики и отчёта |
| `state_snapshots` | полное представление состояния раз в 30 с |
| `state_deltas` | дельта каждого изменения состояния (версия, реальное и модельное время) |

## Сеть и геометрия (1.3.0)

Новых таблиц нет: сеть описывается файлом GeoJSON (см. [network-import.md](network-import.md)), а вычисленная геометрия кэшируется в памяти процесса.

| Где | Что хранится |
|---|---|
| `stations.config.station.geo` | широта, долгота и ось основной станции, источник (`demo`) |
| `stations.config.derived` | вычисленный при заполнении масштаб схемы `schema_scale_u_per_m` (u/м), радиус и угол стрелочного перевода |
| `topology_nodes`, `tracks.points`, `track_connections.points` | плавная геометрия: стрелки — дуги радиуса R, касательные к узлам; `track_connections.length_m` — физическая длина ребра, м |
| `configs/network_demo.geojson` | станции (`detail`: detailed / simplified), перегоны (`tracks`, `length_m` или геодезическая длина, горловины, скорость) |

Модель сети (`GET /api/v1/network`) содержит станции, горловины, перегоны, отдельные пути перегона и проекцию. Она строится функцией `app/domain/network.py::build_network`.
