# Тестирование: проверки и фактические результаты

## Итог последнего запуска (01.10.2026)

| Набор | Где | Результат |
|---|---|---|
| Backend: интеграционные тесты | `backend/tests/` на реальной PostgreSQL (`station_test`, миграции Alembic) | **41 из 41 прошли** (~50 с) |
| Frontend: unit-тесты | `frontend/src/lib/reducer.test.ts` (vitest) | **5 из 5 прошли** |
| Frontend: проверка типов и сборка | `tsc -b`, `vite build` | **без ошибок** |
| Нагрузка и производительность | `tools/bench.py` | см. [performance.md](performance.md) |
| Ручная проверка интерфейса | Chrome, 1564×784 | см. раздел ниже |

Команды запуска:

```bash
docker compose run --rm --no-deps -e ENGINE_ENABLED=false -v ./backend:/app backend python -m pytest tests -q
cd frontend && npm test && npm run build
```

## Что покрыто тестами

| Обязательная проверка | Тест(ы) |
|---|---|
| Правила месячного плана: цель и жёсткая квота | `test_rules.py::test_situation_1_plan_exceeded_soft_then_hard` |
| Ситуация «пути заняты»: отказ, рассчитанное окно, альтернативы | `test_rules.py::test_situation_2_tracks_busy_rejected_with_computed_window` |
| Ситуация «сейчас заполнено, к прибытию освободится» | `test_rules.py::test_situation_3_full_now_frees_before_arrival` |
| Длина состава вместо числа вагонов; «Недостаточно данных» | `test_length_is_checked_not_wagon_count`, `test_insufficient_length_data` |
| Интервальные конфликты, полуоткрытые интервалы | `test_interval_book_half_open`, `test_db_exclusion_constraint_blocks_overlap` |
| Начальный план без пересечений резервов | `test_initial_plan_has_no_reservation_overlaps` |
| Смена конфигурации без изменения логики | `test_small_station_config_same_logic` |
| Допустимые переходы статусов | `test_api.py::test_status_transition_requires_check` |
| Права доступа на backend | `test_permissions_matrix_enforced_on_backend` |
| Единый формат ошибок, сообщения валидации на русском | `test_error_format_and_auth`, `test_validation_error_in_russian` |
| Конкурентное резервирование (2 параллельные заявки) | `test_concurrent_confirm_no_double_booking` |
| Повторная отправка команды (Idempotency-Key) | `test_idempotent_confirm` |
| Отмена заявки и освобождение резервов | `test_cancel_releases_reservations` |
| Конфликтную заявку нельзя подтвердить прямым API-запросом | `test_conflicting_request_cannot_be_confirmed_by_direct_api` |
| Предупреждения требуют подтверждения | `test_warnings_require_acknowledgement` |
| Устаревшая рекомендация не применяется | `test_stale_recommendation_cannot_be_applied` |
| Сквозной сценарий заявки с аудитом | `test_end_to_end_request_confirm_and_audit` |
| Повторное MQTT-сообщение без повторного действия | `test_iot.py::test_duplicate_message_no_repeat_action` |
| Нарушение порядка сообщений | `test_out_of_order_kept_in_history_not_applied` |
| Перезапуск устройства и сброс счётчика | `test_device_reboot_resets_counter` |
| Устаревшее retained-сообщение | `test_stale_retained_message_does_not_refresh` |
| Неверная единица и диапазон | `test_bad_unit_and_range_rejected` |
| Неизвестное устройство, схема, тип события, объект | `test_unknown_device_schema_station_rejected` |
| Противоречивые показания | `test_contradiction_detected_after_grace` |
| Потеря heartbeat | `test_heartbeat_loss_marks_device_offline` |
| Устаревшие данные блокируют подтверждение, после восстановления — разблокируют | `test_stale_data_blocks_confirmation_then_recovers` |
| Поток повышенной интенсивности, защита очереди | `test_high_rate_stream_and_queue_overflow` и нагрузочный бенч |
| Закрытие пути → конфликт → план → применение | `test_planning.py::test_track_closure_conflict_and_replan` |
| 5 и 10 одновременных инцидентов, без двойного резервирования | `test_replan_5_incidents`, `test_replan_10_incidents` |
| Движок не заводит состав на закрытый путь | `test_engine_does_not_enter_closed_track` |
| Ручной перенос операции проверяется сервером | `test_manual_reschedule_validated` |
| Воспроизведение истории совпадает с сохранёнными событиями | `test_replay_reconstructs_saved_state` |
| Восстановление после разрыва соединения (снимок, догон дельт) | `test_websocket_snapshot_and_resume`, `test_replay_reconstructs_saved_state` (`catch_up`) |
| Пересчёт индекса при изменении весов; отсутствующие данные | `test_index_weights_and_missing_data` |
| Помощник отвечает по расчётам и называет недостающие данные | `test_assistant_answers_from_calculations` |
| Отчёт PDF и CSV с кириллицей | `test_reports_cyrillic` |
| Редьюсер дельт, геометрия, интерполяция не дальше конца операции, прогноз занятости | `frontend/src/lib/reducer.test.ts` |

## Проверено вручную, без автоматизации

- **Восстановление брокера** — проверено командой `docker compose restart mqtt` при работающем стеке:
  - backend зафиксировал разрыв (`disconnects: 1`) и переподключился;
  - симулятор записал в журнал «MQTT соединение потеряно; сообщения копятся в буфере», затем «досылка буфера: 57 сообщений»;
  - повторные доставки отсечены по `event_id`: счётчик дублей вырос на 35, бизнес-действия не повторялись;
  - через 15 с все 42 устройства с телеметрией на связи, данные актуальны.

  Автоматического теста перезапуска брокера в наборе нет — это задача бэклога.
- **Интерфейс в Chrome.**
  - Вход по ролям, основной экран: KPI, 2D-схема, 3D-двойник, Гант, панели.
  - Заявки: проверка с отказом и окном 17:45, причины по путям, проверенная альтернатива, объяснения у недоступных кнопок.
  - Найденные дефекты исправлены: наложение подписей зон грузового района (2D и 3D), перекрытие нижнего пути легендой, повторное создание WebGL-контекста.
- **Клавиатура.** Пути и составы на 2D-схеме, полосы на Ганте и строки таблиц доступны по Tab и выбираются Enter. У модальных окон есть фокус и закрытие по Esc.
- **Размеры экрана.** Проверено на 1564×784. Адаптивные правила CSS заданы для ширины ≤ 1100 и ≤ 760 пикселей; на этих размерах вручную не проверялось.

## Чего нет

- Сквозного браузерного e2e-теста (Playwright) нет. Сквозной поток проверяется API-тестами и вручную.
- Автоматического теста отказа и восстановления MQTT-брокера нет.
- Замер отрисовки в браузере на стенде недостоверен: вкладка была в фоне (см. [performance.md](performance.md)).
