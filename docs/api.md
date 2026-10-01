# API

Базовый путь — `/api/v1`. Интерактивная документация OpenAPI (Swagger) с русскими описаниями доступна только администратору — `http://localhost:8080/docs` после входа через пункт меню «API (Swagger)» (см. раздел «Доступ к описанию API»).

## Аутентификация

`POST /auth/login {username, password}` → `{token, user}`. Токен — подписанный HMAC, срок 12 ч. Передаётся в заголовке `Authorization: Bearer <token>`, для WebSocket — параметром `?token=`. Демо-пользователи: `train`, `station`, `duty`, `admin`, `viewer`, пароль `demo123`.

## Единый формат ошибок

```json
{"error": {"code": "REQUEST_NOT_AVAILABLE",
           "message": "Приём 40 ваг. в 14:30 недоступен: нет подходящего свободного пути на время приёма и обработки. Ближайшее допустимое окно — 16:10.",
           "details": {"check": {"...": "..."}},
           "hint": "Выберите одну из проверенных альтернатив или измените параметры заявки."}}
```

| HTTP | Коды |
|---|---|
| 401 | AUTH_REQUIRED, TOKEN_INVALID, TOKEN_EXPIRED, USER_INACTIVE, BAD_CREDENTIALS, SIM_TOKEN_INVALID |
| 403 | FORBIDDEN (в `hint` — какие роли имеют право) |
| 404 | NOT_FOUND, REQUEST_NOT_FOUND, TRACK_NOT_FOUND, TRAIN_NOT_FOUND, PLAN_NOT_FOUND, INCIDENT_NOT_FOUND, DEVICE_NOT_FOUND, RECOMMENDATION_NOT_FOUND, OPERATION_NOT_FOUND, NO_HISTORY |
| 409 | CHECK_REQUIRED, INVALID_STATUS_TRANSITION, REQUEST_NOT_AVAILABLE, WARNINGS_NOT_ACKNOWLEDGED, REQUEST_IN_EXECUTION, RESOURCE_ALREADY_RESERVED, IDEMPOTENCY_KEY_REUSED, REQUEST_IN_PROGRESS, PLAN_STALE, PLAN_INVALID, PLAN_NOT_PROPOSED, PLAN_DISCARDED, RECOMMENDATION_STALE, CONSTRAINTS_VIOLATED, OPERATION_STARTED, NO_ROUTE, ALTERNATIVE_NOT_VALID, SPLIT_NOT_ALLOWED |
| 400 | UNKNOWN_STATION, UNKNOWN_WAGON_KIND, TIME_IN_PAST, UNKNOWN_ALTERNATIVE, UNKNOWN_INCIDENT_KIND, NO_CARGO_OPERATION, INVALID_INDEX_CONFIG, UNKNOWN_SCENARIO, REPLAY_WINDOW_TOO_LONG |
| 422 | VALIDATION_ERROR (`details.fields[]` — поле и сообщение на русском) |
| 503 | STATE_NOT_READY, RESET_BUSY |
| 500 | INTERNAL_ERROR |

## Защита от повторной отправки

Изменяющие команды принимают заголовок `Idempotency-Key`. Если ключ повторяется с тем же телом, возвращается сохранённый ответ с признаком `"idempotent_replay": true`, и действие не выполняется повторно. Тот же ключ с другим телом даёт `409 IDEMPOTENCY_KEY_REUSED`. Ключ сохраняется в той же транзакции, что и действие; ответ с ошибкой не сохраняется, поэтому после отказа команду можно повторить с тем же ключом.

## Основные эндпоинты

| Метод и путь | Право | Назначение |
|---|---|---|
| GET `/health` | — | состояние сервиса, MQTT, число клиентов WebSocket |
| GET `/auth/me`, `/auth/demo-users`, `/permissions` | — | пользователь и права, матрица прав |
| GET `/state` | state.view | полное состояние (то же, что первый снимок WebSocket) |
| GET `/topology` | state.view | узлы, пути, соединения, зоны, устройства, границы схемы; `geometry.schema_scale_u_per_m` — единый масштаб станции (u/м) |
| GET `/network` | state.view | сеть: станции (уровень детализации, горловины, упрощённые параметры), перегоны (длина и её источник, пути перегона с геометрией в метрах ENU), проекция; 422 — ошибки формата данных сети |
| GET `/stations`, `/schedule`, `/trains/{id}`, `/tracks/{id}`, `/capacity` | state.view | справочники, расписание с прогнозом, карточки, ограничения и загрузка |
| GET/POST `/requests` | state.view / request.create | список и создание заявок |
| POST `/requests/{id}/check` | request.check | проверка приёма |
| POST `/requests/{id}/confirm {acknowledge_warnings}` | request.confirm | повторная проверка и резервирование |
| POST `/requests/{id}/cancel`, `/reject {reason}` | request.cancel / request.reject | отмена с освобождением резервов, отказ |
| POST `/requests/{id}/reschedule {departure}` | request.reschedule | перенос с перепроверкой |
| POST `/requests/{id}/apply-alternative {action}` | зависит от типа | применение проверенной альтернативы |
| POST `/plans/compute`; GET `/plans`, `/plans/{id}`; POST `/plans/{id}/apply` | plan.compute / plan.apply | перепланирование |
| POST `/operations/{id}/reschedule {start, track_id?, reason}` | operation.reschedule | ручной перенос |
| GET `/recommendations`; POST `/recommendations/{id}/apply` | recommendation.apply | рекомендации |
| GET/POST `/incidents`; POST `/incidents/{id}/resolve` | incident.manage | инциденты |
| GET `/devices`, `/devices/{id}`, `/telemetry/rejects` | state.view | реестр устройств, история, отклонённые сообщения |
| POST `/observations/override`, `/observations/override/{id}/cancel` | observation.override | ручное уточнение |
| GET `/index`, `/index/config`, `/index/history`; PUT `/index/config` | config.manage | индекс эффективности |
| PUT `/config/plan-policy`, `/config/device-thresholds`; GET `/config/thresholds` | config.manage | политики и пороги |
| GET `/sim`; POST `/sim/start`, `/sim/pause`, `/sim/speed`, `/sim/reset` | sim.control | симуляция и сценарии |
| GET `/audit`, `/events` | state.view | журналы |
| GET `/replay/range`, `/replay/window?from&to` | state.view | история для перемотки (окно ≤ 20 мин) |
| GET `/reports/mini?format=pdf\|csv&minutes=60` | report.export | мини-отчёт |
| POST `/assistant/ask {question, context}` | assistant.ask | помощник |
| GET `/metrics`; POST `/metrics/client` | state.view | метрики p50/p95/max |

## WebSocket `/ws?token=…`

| Направление | Сообщение |
|---|---|
| сервер → клиент | `{"type":"snapshot","version":N,"state":{…},"server_time":t}` |
| сервер → клиент | `{"type":"delta","version":N,"base":N-1,"delta":{"<коллекция>":{"upsert":{…},"remove":[…]},"meta":{…}},"trace":{"event_received_at":t}}` |
| сервер → клиент | `{"type":"ping","server_time":t}` — каждые 10 с без данных |
| клиент → сервер | `{"type":"hello","last_version":N}` (−1 — нужен снимок), `{"type":"pong","server_time":t}`, `{"type":"metrics","samples":[…]}` |

Клиент применяет дельту, только если `base` равен его текущей версии; иначе запрашивает снимок. Неверный токен закрывает соединение с кодом 4401.

## Пример главного сценария

```bash
API=http://localhost:8000/api/v1
TOK() { curl -s -X POST $API/auth/login -H 'content-type: application/json' \
  -d "{\"username\":\"$1\",\"password\":\"demo123\"}" | python -c "import sys,json;print(json.load(sys.stdin)['token'])"; }
ADMIN=$(TOK admin); DUTY=$(TOK duty)

# Ситуация 2: пути заняты на время прибытия
curl -s -X POST $API/sim/reset -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' \
  -d '{"scenario":"demo_tracks_busy","seed":42}'
curl -s -X POST $API/requests/RQ-Z-0001/check -H "Authorization: Bearer $DUTY"
# → "decision": "unavailable",
#   "summary": "Приём 40 ваг. в 14:30 недоступен: нет подходящего свободного пути на время приёма и обработки. Ближайшее допустимое окно — …",
#   "items": [{"code":"MONTHLY_PLAN","status":"ok",…}, {"code":"TIME_WINDOW","status":"fail",…}],
#   "tracks": [{"track_id":"ALM-T6","code":"LENGTH","message":"Путь 6: полезная длина 560 м меньше требуемой 600.8 м …"}, …],
#   "nearest_window": {"arrival":"…","departure":"…","track_id":"ALM-T4","delay_min":…},
#   "alternatives": [{"type":"postpone","verified":true,"effect":{"delay_min":…,"load_change":"…"}, "action":{…}}]

# Прямое подтверждение конфликтной заявки отклоняется сервером
curl -s -X POST $API/requests/RQ-Z-0001/confirm -H "Authorization: Bearer $DUTY" \
  -H 'content-type: application/json' -H "Idempotency-Key: $(uuidgen)" -d '{"acknowledge_warnings":true}'
# → 409 {"error":{"code":"REQUEST_NOT_AVAILABLE", …}}
```

Время в ответах указано в ISO 8601 UTC (`…Z`); интерфейс показывает его в часовом поясе станции.

## Администрирование: роли и пользователи (только администратор)

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/v1/admin/roles` | роли, их права и число пользователей |
| POST | `/api/v1/admin/roles` | создать роль `{id, name, description, permissions[]}` |
| PUT | `/api/v1/admin/roles/{id}` | изменить пользовательскую роль (название, описание, права, `active`) |
| DELETE | `/api/v1/admin/roles/{id}` | удалить пользовательскую роль, если она никому не назначена |
| GET | `/api/v1/admin/users` | пользователи |
| POST | `/api/v1/admin/users` | создать пользователя `{username, full_name, role, password}` (пароль ≥ 8 символов) |
| PUT | `/api/v1/admin/users/{id}` | сменить роль, имя, заблокировать или разблокировать (`active`) |
| POST | `/api/v1/admin/users/{id}/password` | задать пароль |

Коды ошибок:
- `FORBIDDEN` — роль без `users.manage`;
- `PERMISSION_RESERVED` — попытка выдать роли право управлять ролями;
- `SYSTEM_ROLE_READONLY` — изменение системной роли;
- `ROLE_EXISTS`, `ROLE_NAME_EXISTS` — роль с таким идентификатором или названием уже есть;
- `ROLE_IN_USE` — удаление назначенной роли;
- `UNKNOWN_ROLE`, `UNKNOWN_PERMISSION` — неизвестная роль или право;
- `USER_EXISTS` — логин занят;
- `BAD_ROLE_ID`, `BAD_USERNAME` — недопустимый формат;
- `LAST_ADMIN` — нельзя убрать последнего администратора.

Пример:

```bash
curl -X POST http://localhost:8080/api/v1/admin/roles -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{"id":"shift_master","name":"Сменный мастер","permissions":["incident.manage","request.check"]}'
```

## Доступ к описанию API

Стандартные `/docs`, `/redoc` и `/openapi.json` FastAPI отключены. Swagger и схема OpenAPI доступны **только администратору** (право `api.docs`, его нельзя выдать пользовательской роли):

1. Интерфейс вызывает `POST /api/v1/auth/docs-session` с токеном администратора. Сервер ставит HttpOnly-cookie `ds_docs` (SameSite=Strict, 1 ч).
2. Затем открывается `/docs`; `/openapi.json` проверяет ту же cookie или заголовок `Authorization`.

Без прав администратора `/docs` возвращает страницу 403, а `/openapi.json` — ошибку `FORBIDDEN`. `/redoc` не существует.

Наружу (в сеть) публикуется только порт интерфейса 8080. Backend (8000) и PostgreSQL (5432) привязаны к `127.0.0.1`, поэтому закрытие Swagger нельзя обойти прямым обращением к backend из сети. MQTT 1883 доступен в сети для подключения устройств: анонимный доступ запрещён, права на топики заданы ACL.


## Процесс работников (мобильный раздел)

Подробно — [mobile.md](mobile.md). Все команды проверяют на сервере:
- право;
- область доступа (станция / ПТО / бригада);
- назначение;
- статус.

Чужие объекты возвращают 404. Изменяющие команды принимают `Idempotency-Key` и `expected_version`; при несовпадении версии — 409 `STALE_VERSION`.

| Метод и путь | Право | Назначение |
|---|---|---|
| GET `/mobile/context` | mobile.access | пользователь, роль, права, привязка, счётчики, справочники |
| GET `/mobile/wagons?q=` · `/mobile/trains` · `/mobile/tracks` | defect.create / triage / view_station / work_order.execute | поиск вагона, выбор из состава, пути своей станции |
| POST `/defect-reports` | defect.create | сообщение; идемпотентно по `client_uuid`; критичное — ограничение вагона |
| GET `/defect-reports?scope=own\|station&status=&urgency=&track_id=&wagon=&page=` | view_own / view_station | списки с фильтрами и пагинацией |
| GET `/defect-reports/{id}` | видимость | карточка: вагон, фото, хронология, работы, доступные действия (`allowed`) |
| POST `/defect-reports/{id}/actions/{acknowledge\|review\|needs_info\|reply\|reject\|duplicate\|link_wagon\|comment}` | по матрице | действия с сообщением |
| POST `/defect-reports/{id}/decision` | work_order.create (+ wagon_replacement.approve для замены) | принять и создать заявку |
| POST `/defect-reports/{id}/work-orders` | work_order.create | дополнительная заявка по принятому сообщению |
| GET `/work-orders?scope=mine\|inspect\|station` · `/work-orders/{id}` · `/work-orders/executors` | execute / inspect / assign / view_station | задания, карточка, исполнители |
| POST `/work-orders/{id}/actions/{assign\|start\|pause\|resume\|submit\|accept\|rework\|cancel\|comment}` | по матрице | ход работ, контрольный осмотр, отмена |
| GET `/wagons/{id}/replacement-candidates` | wagon_replacement.approve | подбор исправного вагона с причинами непригодности |
| POST `/attachments?client_uuid=&filename=` (тело — файл) · GET / DELETE `/attachments/{id}` | загрузчик; доступ к записи | фото JPEG/PNG/WebP до 8 МБ |
| GET `/notifications` · POST `/notifications/read` | вход | уведомления пользователя |

Роли без `state.view` получают по `/ws` ограниченный канал: `hello_ok` и события `work`. Снимков и дельт станции в нём нет.
