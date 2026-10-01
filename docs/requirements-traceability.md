# Соответствие требованиям презентации

Проверки: `backend/tests/test_rules.py`, `test_api.py`, `test_iot.py`, `test_planning.py` (реальная PostgreSQL), `frontend/src/lib/reducer.test.ts`, замеры `tools/bench.py`, ручная проверка интерфейса в браузере (см. testing.md).

| Требование | Компонент | Критерий приёмки | Подтверждающая проверка |
|---|---|---|---|
| UI/UX: понятный основной экран | `Overview.tsx`, `Header.tsx` | шапка (станция, время, режим, связь), 3D-двойник, карточка, Гант, конфликты | ручная проверка в браузере |
| Цвет дублируется текстом/значком | `lib/labels.ts`, `twin/palette.ts`, `twin/StationLevel.tsx` | каждый статус имеет значок и подпись | ручная проверка, легенда схемы |
| Недоступная кнопка объясняет причину | `components/ui.tsx::ActionButton` | рядом с кнопкой текст причины | ручная проверка (раздел «Заявки») |
| Причина отказа и альтернативы | `checker.py`, `alternatives.py`, `Requests.tsx` | расчётное окно и только проверенные альтернативы с последствиями | `test_situation_2_tracks_busy_rejected_with_computed_window` |
| Три ситуации главного сценария | `scenarios.py`, `checker.py` | предупреждение/квота; отказ с окном; допуск, когда путь освободится к прибытию | `test_situation_1_plan_exceeded_soft_then_hard`, `test_situation_2_…`, `test_situation_3_full_now_frees_before_arrival` |
| Длина, а не число вагонов; «Недостаточно данных» | `placement.py` | путь короче состава отклоняется; без длины — insufficient_data | `test_length_is_checked_not_wagon_count`, `test_insufficient_length_data` |
| Конфигурации малой и крупной станции | `configs/*.json`, `topology.py` | одна бизнес-логика для обеих | `test_small_station_config_same_logic` |
| Интервальные конфликты | `IntervalBook`, ограничение-исключение | полуоткрытые интервалы; пересечение запрещено в БД | `test_interval_book_half_open`, `test_db_exclusion_constraint_blocks_overlap`, `test_initial_plan_has_no_reservation_overlaps` |
| Права проверяются на backend | `core/permissions.py` | 403 для каждой изменяющей операции без права | `test_permissions_matrix_enforced_on_backend` |
| Допустимые переходы статусов | `domain/statuses.py` | подтверждение без проверки запрещено | `test_status_transition_requires_check` |
| Конкурентное резервирование | advisory lock + EXCLUDE | из двух одновременных подтверждений одного ресурса проходит одно | `test_concurrent_confirm_no_double_booking` |
| Повторная отправка команды | `core/idempotency.py` | повтор не создаёт второго поезда; другой запрос с тем же ключом — 409 | `test_idempotent_confirm` |
| Отмена заявки освобождает резервы | `services/requests.py::cancel` | резервы `released`, операции отменены | `test_cancel_releases_reservations` |
| Конфликтная заявка не подтверждается прямым API | `requests.py::confirm` | 409 REQUEST_NOT_AVAILABLE, резервов нет | `test_conflicting_request_cannot_be_confirmed_by_direct_api` |
| Последствия перед подтверждением | `ConfirmDialog`, `WARNINGS_NOT_ACKNOWLEDGED` | с предупреждениями нужно явное согласие | `test_warnings_require_acknowledgement` |
| Сквозной сценарий заявки и аудит | API + БД | создание → проверка → подтверждение → резервы → аудит | `test_end_to_end_request_confirm_and_audit` |
| Единый формат ошибок на русском | `core/errors.py` | `{error:{code,message,details,hint}}` | `test_error_format_and_auth`, `test_validation_error_in_russian` |
| Устаревшая рекомендация | `routes.py::apply_recommendation` | 409 RECOMMENDATION_STALE после изменения состояния | `test_stale_recommendation_cannot_be_applied` |
| ИИ-планирование: закрытие пути → конфликт → план | `conflicts.py`, `planner.py`, `plans.py` | конфликт обнаружен, план допустим (валидатор), применён без двойных резервов | `test_track_closure_conflict_and_replan` |
| Сквозной сценарий показа | весь стек | все шаги docs/demo.md выполняются на запущенном стенде | `tools/demo_e2e.py` (0 ошибок) |
| Качество плана: изменения только затронутых операций | `seed.py::optimize_initial_plan`, штраф смены ресурса | после оптимизации базы пересчёт меняет ≤ 5 операций без инцидента | `test_initial_plan_optimization_reproducible_and_cached` |
| Пересчёт ≤ 5 с; 5 и 10 инцидентов | `planner.py` (лимит 3,5 с) | статус честный, план проверен, нет двойного резервирования | `test_replan_5_incidents`, `test_replan_10_incidents`, `tools/bench.py` |
| Движение не проходит через запрещённое | `sim/engine.py` | заезд на закрытый путь не начинается, поезд ждёт с причиной | `test_engine_does_not_enter_closed_track` |
| Исполнение плана моделью | `sim/engine.py` | операции выполняются, пересечений резервов нет | `test_engine_progresses_trains` |
| Ручной перенос с проверкой | `plans.py::reschedule_operation` | нарушающий перенос — 409 со списком ошибок | `test_manual_reschedule_validated` |
| Realtime и восстановление после разрыва | `hub.py`, `ws.py`, `lib/ws.ts` | снимок при подключении, досылка дельт по версии | `test_websocket_snapshot_and_resume`, `test_replay_reconstructs_saved_state` (catch_up) |
| История соответствует сохранённым событиям | `state_snapshots/deltas`, `apply_delta` | реконструкция = итоговое состояние | `test_replay_reconstructs_saved_state`, `reducer.test.ts` |
| Индекс: формула, веса, отсутствующие данные | `services/index.py` | пересчёт при смене весов; без данных — исключение и «частичное» качество | `test_index_weights_and_missing_data` |
| Помощник объясняет расчёты, называет недостающие данные | `services/assistant.py` | 5 типов вопросов, факты из расчёта | `test_assistant_answers_from_calculations` |
| Мини-отчёт PDF/CSV с кириллицей | `services/reports.py` | BOM в CSV, корректный PDF | `test_reports_cyrillic` |
| IoT: повторное сообщение | `iot/ingest.py` | без повторного действия | `test_duplicate_message_no_repeat_action` |
| IoT: нарушение порядка | `ingest.py` | в истории, не заменяет новое | `test_out_of_order_kept_in_history_not_applied` |
| IoT: перезапуск устройства | `ingest.py` | новый boot_id сбрасывает счётчик | `test_device_reboot_resets_counter` |
| IoT: устаревшее retained-сообщение | `ingest.py` | не «освежает» данные | `test_stale_retained_message_does_not_refresh` |
| IoT: неверная единица и диапазон | `iot/contract.py` | отклонение с кодом; м/с → км/ч | `test_bad_unit_and_range_rejected` |
| IoT: неизвестное устройство, схема, тип, объект | `ingest.py` | отклонение с кодом | `test_unknown_device_schema_station_rejected` |
| Потеря данных → «Неизвестно» → блокировка → восстановление | `iot/quality.py`, `checker.py` | путь DATA, приём недоступен; после свежих данных доступен | `test_stale_data_blocks_confirmation_then_recovers` |
| Противоречивые показания | `quality.py` | противоречие после 6 с, оба наблюдения | `test_contradiction_detected_after_grace` |
| Потеря heartbeat | `quality.py::device_statuses` | связь offline отдельно от качества | `test_heartbeat_loss_marks_device_offline` |
| Поток повышенной интенсивности, защита очереди | `iot/runtime.py` | значимые раньше координат, координаты объединяются, переполнение отклоняется | `test_high_rate_stream_and_queue_overflow`, `tools/bench.py` |
| Восстановление брокера | постоянная сессия MQTT, буфер симулятора | после перезапуска `mqtt` приём продолжается | ручная проверка (testing.md) |
| Backend: модульность, история, прозрачная логика | `app/services/*`, аудит | модули по ответственности, журнал событий и аудит | architecture.md, раздел «Журнал» |
| Демо и инженерная культура | README, docker-compose, тесты, метрики | запуск одной командой, `GET /metrics`, JSON-логи | README.md, testing.md, performance.md |
