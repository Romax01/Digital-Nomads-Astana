"""Схемы входных данных API (описания полей — на русском для OpenAPI)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str = Field(description="Имя пользователя")
    password: str = Field(description="Пароль")


class RequestCreate(BaseModel):
    from_station_id: str = Field(description="Станция отправления (соседняя станция модели)", examples=["OTR"])
    wagons_count: int = Field(gt=0, le=120, description="Количество вагонов, шт.")
    wagon_kind: str | None = Field(default=None, description="Род вагонов (gondola, covered, tank, flat, hopper) — "
                                                             "определяет длину вагона", examples=["gondola"])
    train_length_m: float | None = Field(default=None, gt=0, le=1500, description="Длина состава с локомотивом, м")
    cargo: str | None = Field(default=None, description="Груз")
    priority: int = Field(default=2, ge=1, le=5, description="Приоритет 1 (низкий) … 5 (высший)")
    split_allowed: bool = Field(default=False, description="Разрешено ли разделение партии")
    desired_departure: datetime = Field(description="Желаемое время отправления со станции отправления (ISO 8601 с поясом)")


class ConfirmIn(BaseModel):
    acknowledge_warnings: bool = Field(default=False, description="С предупреждениями ознакомлен")


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500, description="Основание")


class RescheduleIn(BaseModel):
    departure: datetime = Field(description="Новое время отправления")


class AlternativeIn(BaseModel):
    action: dict = Field(description="Действие альтернативы из результата проверки (поле action)")


class OperationRescheduleIn(BaseModel):
    start: datetime = Field(description="Новое начало операции")
    track_id: str | None = Field(default=None, description="Новый путь стоянки (необязательно)")
    reason: str = Field(default="Ручной перенос диспетчером", max_length=300)


class IncidentIn(BaseModel):
    kind: Literal["track_closure", "switch_failure", "cargo_delay", "faulty_wagon", "neighbor_restriction",
                  "resource_failure"] = Field(description="Вид инцидента")
    object_id: str | None = Field(default=None, description="Объект: путь, стрелка, вагон, ресурс, соседняя станция")
    duration_min: int | None = Field(default=None, gt=0, le=24 * 60, description="Длительность, мин (пусто — до отмены)")
    extra_min: int | None = Field(default=None, gt=0, le=600, description="Дополнительная задержка, мин (для cargo_delay)")
    title: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=1000)


class OverrideIn(BaseModel):
    object_id: str = Field(description="Путь, для которого уточняется занятость")
    occupied: bool = Field(description="Уточнённое значение: занят (true) / свободен (false)")
    reason: str = Field(min_length=5, max_length=500, description="Основание (обязательно)")
    valid_minutes: int = Field(ge=1, le=120, description="Срок действия, мин реального времени")


class SpeedIn(BaseModel):
    speed: float = Field(gt=0, le=120, description="Множитель модельного времени")


class ResetIn(BaseModel):
    scenario: str = Field(default="normal")
    seed: int = Field(default=42, ge=0, le=10 ** 9)
    station_config: Literal["large", "small"] | None = None


class IndexConfigIn(BaseModel):
    weights: dict[str, float]
    params: dict
    thresholds: dict[str, float]
    max_data_age_s: int = 30
    reason: str = Field(default="Изменение конфигурации индекса", max_length=300)


class PolicyIn(BaseModel):
    policy: Literal["soft", "hard_quota"] = Field(description="soft — целевой показатель, hard_quota — жёсткая квота")
    reason: str = Field(default="Изменение политики месячного плана", max_length=300)


class ThresholdIn(BaseModel):
    kind: str = Field(description="Тип источника (track_circuit, switch_sensor, …)")
    stale_after_s: float = Field(gt=0, le=3600, description="Порог устаревания, с")


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    context: dict = Field(default_factory=dict, description="Контекст: request_id, track_id, train_id")


class ClientMetricsIn(BaseModel):
    samples: list[dict] = Field(default_factory=list)


class RoleCreateIn(BaseModel):
    id: str = Field(min_length=3, max_length=32, description="Идентификатор роли (латиница, цифры, «_»)", examples=["shift_master"])
    name: str = Field(min_length=3, max_length=80, description="Название роли", examples=["Сменный мастер"])
    description: str = Field(default="", max_length=500)
    permissions: list[str] = Field(default_factory=list, description="Права (коды из матрицы прав)")


class RoleUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    permissions: list[str] | None = None
    active: bool | None = None


class UserCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, description="Логин")
    full_name: str = Field(min_length=2, max_length=120, description="ФИО / подпись в журнале")
    role: str = Field(description="Идентификатор роли")
    password: str = Field(min_length=8, max_length=128, description="Пароль, не менее 8 символов")


class UserUpdateIn(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    role: str | None = None
    active: bool | None = None


class PasswordIn(BaseModel):
    password: str = Field(min_length=8, max_length=128, description="Новый пароль, не менее 8 символов")
