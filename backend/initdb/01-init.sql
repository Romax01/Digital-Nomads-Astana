-- Расширение btree_gist нужно для ограничения-исключения (запрет пересечения броней).
CREATE EXTENSION IF NOT EXISTS btree_gist;
-- Отдельная база для интеграционных тестов.
CREATE DATABASE station_test;
\c station_test
CREATE EXTENSION IF NOT EXISTS btree_gist;
