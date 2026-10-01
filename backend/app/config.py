"""Настройки приложения. Все секреты и адреса берутся из переменных окружения."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://station:station_dev_password@localhost:5432/station"
    secret_key: str = "change_me_dev_secret"
    token_ttl_hours: int = 12

    station_config: str = "large"  # large | small
    display_timezone: str = "Asia/Almaty"

    mqtt_enabled: bool = True
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_user: str = "ingest"
    mqtt_password: str = "ingest_dev_password"
    mqtt_topic: str = "station/#"

    sim_world_token: str = "sim_world_dev_token"
    sim_autostart: bool = True
    sim_real_time: bool = False  # заполнение при первом запуске в режиме реального времени
    sim_seed: int = 42
    sim_tick_seconds: float = 1.0
    seed_optimize: bool = True  # оптимизировать начальный план рабочих сценариев (детерминированно)

    planner_time_limit_s: float = 3.5
    planner_workers: int = 8
    replan_debounce_s: float = 1.0

    telemetry_retention_hours: int = 48
    replay_retention_minutes: int = 60
    snapshot_every_s: int = 30
    audit_retention_days: int = 365

    ingest_queue_max: int = 20000

    llm_provider: str = ""
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-5-5"

    engine_enabled: bool = True  # в тестах движок симуляции не запускается фоном


@lru_cache
def get_settings() -> Settings:
    return Settings()
