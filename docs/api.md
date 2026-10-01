# API

Базовый путь — `/api/v1`. Интерактивная документация OpenAPI (Swagger) с русскими описаниями доступна по адресам `http://localhost:8000/docs` и `http://localhost:8080/docs` (через nginx).

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
| GET `/topology` | state.view | узлы, пути, соединения, зоны, устройства, границы схемы |
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
