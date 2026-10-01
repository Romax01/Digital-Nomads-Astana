"""ORM-модель цифрового двойника.

Единицы измерения: длины — метры, длительности — минуты, время — timestamptz (UTC в БД,
отображение в часовом поясе станции). Модельное время симуляции хранится в полях *_at
операций и поездов; реальное время — в полях ts/received_at/created_at служебных таблиц.
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

J = JSON().with_variant(JSONB(), "postgresql")
TS = DateTime(timezone=True)


# ---------------------------------------------------------------- топология и станции
class Station(Base):
    __tablename__ = "stations"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16))  # main | neighbor
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Almaty")
    config: Mapped[dict] = mapped_column(J, default=dict)


class Park(Base):
    __tablename__ = "parks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(32))  # receiving_departure | sorting | cargo | repair | main


class Zone(Base):
    __tablename__ = "zones"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(32))  # cargo_front | repair | platform | loco_depot | inspection
    track_ids: Mapped[list] = mapped_column(J, default=list)
    x: Mapped[float] = mapped_column(Float, default=0)
    y: Mapped[float] = mapped_column(Float, default=0)
    params: Mapped[dict] = mapped_column(J, default=dict)


class TopologyNode(Base):
    __tablename__ = "topology_nodes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    kind: Mapped[str] = mapped_column(String(16))  # switch | entry | exit | end
    name: Mapped[str] = mapped_column(String(64))
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)  # west | east


class Track(Base):
    __tablename__ = "tracks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    park_id: Mapped[str] = mapped_column(ForeignKey("parks.id"))
    number: Mapped[str] = mapped_column(String(8))
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))  # receiving_departure | sorting | cargo | repair | main
    useful_length_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    allowed_train_kinds: Mapped[list] = mapped_column(J, default=list)
    from_node: Mapped[str] = mapped_column(String(32))
    to_node: Mapped[str] = mapped_column(String(32))
    zone_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    points: Mapped[list] = mapped_column(J, default=list)
    electrified: Mapped[bool] = mapped_column(Boolean, default=True)


class TrackConnection(Base):
    """Ребро графа топологии. Если track_id задан — ребро является путём станции,
    иначе это соединение в горловине (стрелочная связь) или входной участок."""

    __tablename__ = "track_connections"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    from_node: Mapped[str] = mapped_column(String(32))
    to_node: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # track | link | approach
    track_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    length_m: Mapped[float] = mapped_column(Float)
    points: Mapped[list] = mapped_column(J, default=list)


# ---------------------------------------------------------------- поезда и операции
class Train(Base):
    __tablename__ = "trains"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    number: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))  # freight | passenger | transfer
    priority: Mapped[int] = mapped_column(Integer, default=2)  # 1 (низкий) … 5 (высший)
    origin_station_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    destination_station_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    arrival_side: Mapped[str] = mapped_column(String(8), default="west")
    departure_side: Mapped[str] = mapped_column(String(8), default="east")
    wagons_count: Mapped[int] = mapped_column(Integer, default=0)
    length_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    loco_length_m: Mapped[float] = mapped_column(Float, default=34.0)
    status: Mapped[str] = mapped_column(String(16), default="scheduled")
    scheduled_arrival: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    expected_arrival: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    scheduled_departure: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    expected_departure: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    current_track_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    transfer_request_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cargo: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Wagon(Base):
    __tablename__ = "wagons"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    train_id: Mapped[str | None] = mapped_column(ForeignKey("trains.id"), nullable=True)
    number: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))  # gondola | covered | tank | flat | hopper | passenger
    length_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    loaded: Mapped[bool] = mapped_column(Boolean, default=False)
    condition: Mapped[str] = mapped_column(String(16), default="ok")  # ok | faulty | in_repair
    position: Mapped[int] = mapped_column(Integer, default=0)
    last_checkpoint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class Operation(Base):
    """Технологическая операция. Для манёвров track_id — путь назначения, from_track_id — исходный."""

    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    train_id: Mapped[str | None] = mapped_column(ForeignKey("trains.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    # arrival | inspection | shunting | loading | unloading | repair | departure | dwell
    seq: Mapped[int] = mapped_column(Integer, default=0)
    track_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    from_track_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)  # сторона горловины для движения
    duration_min: Mapped[int] = mapped_column(Integer)
    requirements: Mapped[list] = mapped_column(J, default=list)  # виды ресурсов
    resource_ids: Mapped[list] = mapped_column(J, default=list)
    route_nodes: Mapped[list] = mapped_column(J, default=list)  # стрелки маршрута
    planned_start: Mapped[datetime] = mapped_column(TS)
    planned_end: Mapped[datetime] = mapped_column(TS)
    not_before: Mapped[datetime | None] = mapped_column(TS, nullable=True)  # раньше начать нельзя
    forecast_start: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    forecast_end: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    actual_start: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    actual_end: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="planned")
    extra_delay_min: Mapped[int] = mapped_column(Integer, default=0)  # задержка от инцидентов
    reserved: Mapped[bool] = mapped_column(Boolean, default=True)  # False — требование ещё не спланировано
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (Index("ix_operations_train", "train_id", "seq"),)


class Resource(Base):
    __tablename__ = "resources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    kind: Mapped[str] = mapped_column(String(24))
    # shunting_loco | loco_crew | shunting_crew | inspection_team | cargo_equipment | repair_team
    name: Mapped[str] = mapped_column(String(64))
    home_zone_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="available")  # available | faulty
    params: Mapped[dict] = mapped_column(J, default=dict)


class ResourceShift(Base):
    __tablename__ = "resource_shifts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"))
    start_at: Mapped[datetime] = mapped_column(TS)
    end_at: Mapped[datetime] = mapped_column(TS)


class MaintenanceWindow(Base):
    __tablename__ = "maintenance_windows"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    object_type: Mapped[str] = mapped_column(String(16))  # track | resource | switch
    object_id: Mapped[str] = mapped_column(String(32))
    start_at: Mapped[datetime] = mapped_column(TS)
    end_at: Mapped[datetime] = mapped_column(TS)
    reason: Mapped[str] = mapped_column(String(255))


# ---------------------------------------------------------------- ограничения и план
class CapacityRule(Base):
    __tablename__ = "capacity_rules"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    code: Mapped[str] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(32))
    # monthly_plan | simultaneous_capacity | throughput | processing_capacity | resource | time_window
    unit: Mapped[str] = mapped_column(String(48))
    period: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(160))
    rule_text: Mapped[str] = mapped_column(Text)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    policy: Mapped[str] = mapped_column(String(16), default="hard")  # hard | soft
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Plan(Base):
    """Месячный план обработки вагонов: целевой показатель, не физическое ограничение."""

    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    month: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    target_wagons: Mapped[int] = mapped_column(Integer)
    actual_base_wagons: Mapped[int] = mapped_column(Integer, default=0)  # факт до начала симуляции
    policy: Mapped[str] = mapped_column(String(16), default="soft")  # soft (цель) | hard_quota


class TransferRequest(Base):
    __tablename__ = "transfer_requests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    number: Mapped[str] = mapped_column(String(16), unique=True)
    from_station_id: Mapped[str] = mapped_column(String(16))
    to_station_id: Mapped[str] = mapped_column(String(16))
    wagons_count: Mapped[int] = mapped_column(Integer)
    wagon_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    train_length_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    cargo: Mapped[str | None] = mapped_column(String(64), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=2)
    split_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    desired_departure: Mapped[datetime] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(16), default="new")
    last_check: Mapped[dict | None] = mapped_column(J, nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_check_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    train_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class Reservation(Base):
    """Резерв ресурса на интервал [start_at, end_at).

    Ключ resource_key: track:<id> | switch:<id> | res:<id> | neighbor:<id>.
    Пересечение активных резервов одного ключа запрещено ограничением-исключением
    в БД (см. миграцию), а не только кодом приложения.
    """

    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(String(16))
    resource_key: Mapped[str] = mapped_column(String(64))
    start_at: Mapped[datetime] = mapped_column(TS)
    end_at: Mapped[datetime] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(16), default="confirmed")  # confirmed | released
    purpose: Mapped[str] = mapped_column(String(160))
    train_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS)

    __table_args__ = (Index("ix_res_key_time", "resource_key", "start_at"),)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    # track_closure | cargo_delay | faulty_wagon | neighbor_restriction | resource_failure | sensor_loss
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    object_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    start_at: Mapped[datetime] = mapped_column(TS)
    end_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | resolved
    severity: Mapped[str] = mapped_column(String(16), default="high")
    params: Mapped[dict] = mapped_column(J, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS)


class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))  # apply_plan | postpone_departure | ...
    title: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    affected: Mapped[list] = mapped_column(J, default=list)
    action: Mapped[dict] = mapped_column(J, default=dict)
    effect: Mapped[dict] = mapped_column(J, default=dict)
    computed_at: Mapped[datetime] = mapped_column(TS)  # модельное время расчёта
    computed_real_at: Mapped[datetime] = mapped_column(TS)
    based_on_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")


class PlanVersion(Base):
    """Результат планировщика: предложенный план и его сравнение с текущим."""

    __tablename__ = "plan_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TS)
    model_time: Mapped[datetime] = mapped_column(TS)
    base_state_version: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="proposed")  # proposed | applied | stale | rejected
    solver: Mapped[str] = mapped_column(String(32))
    solver_status: Mapped[str] = mapped_column(String(32))
    solve_ms: Mapped[float] = mapped_column(Float)
    summary: Mapped[dict] = mapped_column(J, default=dict)
    changes: Mapped[list] = mapped_column(J, default=list)
    assignments: Mapped[list] = mapped_column(J, default=list)
    assumptions: Mapped[list] = mapped_column(J, default=list)
    applied_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


# ---------------------------------------------------------------- пользователи и аудит
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Role(Base):
    """Роль. Системные роли (system=True) описаны в коде; пользовательские создаёт администратор."""

    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    permissions: Mapped[list] = mapped_column(J, default=list)
    system: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TS)
    model_time: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    before: Mapped[dict | None] = mapped_column(J, nullable=True)
    after: Mapped[dict | None] = mapped_column(J, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(160))
    request_hash: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response: Mapped[dict | None] = mapped_column(J, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS)


class DomainEvent(Base):
    """Журнал доменных событий (для раздела «Журнал событий» и отчёта)."""

    __tablename__ = "domain_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TS)
    model_time: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    type: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(J, default=dict)


# ---------------------------------------------------------------- IoT
class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(32))
    # track_circuit | rfid_reader | loco_gps | switch_sensor | cargo_equipment | repair_diag
    station_id: Mapped[str] = mapped_column(String(16))
    object_id: Mapped[str] = mapped_column(String(48))
    allowed_event_types: Mapped[list] = mapped_column(J, default=list)
    period_s: Mapped[float] = mapped_column(Float, default=2.0)
    stale_after_s: Mapped[float] = mapped_column(Float, default=10.0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | disabled
    source_mode: Mapped[str] = mapped_column(String(16), default="simulator")
    x: Mapped[float] = mapped_column(Float, default=0)
    y: Mapped[float] = mapped_column(Float, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_boot_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    health: Mapped[dict] = mapped_column(J, default=dict)


class TelemetryEvent(Base):
    __tablename__ = "telemetry_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64))
    device_id: Mapped[str] = mapped_column(String(48))
    station_id: Mapped[str] = mapped_column(String(16))
    object_id: Mapped[str] = mapped_column(String(48))
    event_type: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(TS)
    received_at: Mapped[datetime] = mapped_column(TS)
    sequence_number: Mapped[int] = mapped_column(BigInteger)
    boot_id: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(J)
    quality: Mapped[str] = mapped_column(String(16))
    source_mode: Mapped[str] = mapped_column(String(16))
    schema_version: Mapped[str] = mapped_column(String(8))
    disposition: Mapped[str] = mapped_column(String(16))  # applied | late | stale | duplicate_ignored
    __table_args__ = (
        UniqueConstraint("device_id", "event_id", name="uq_telemetry_device_event"),
        Index("ix_telemetry_received", "received_at"),
        Index("ix_telemetry_object", "object_id", "observed_at"),
    )


class TelemetryReject(Base):
    __tablename__ = "telemetry_rejects"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    received_at: Mapped[datetime] = mapped_column(TS)
    topic: Mapped[str] = mapped_column(String(160))
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(48))
    reason: Mapped[str] = mapped_column(Text)
    raw: Mapped[str] = mapped_column(Text)


class Observation(Base):
    """Последнее принятое наблюдение по объекту и признаку (наблюдаемое состояние)."""

    __tablename__ = "observations"
    object_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    attribute: Mapped[str] = mapped_column(String(32), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    value: Mapped[dict] = mapped_column(J)
    observed_at: Mapped[datetime] = mapped_column(TS)
    received_at: Mapped[datetime] = mapped_column(TS)
    quality: Mapped[str] = mapped_column(String(16))
    event_id: Mapped[str] = mapped_column(String(64))


class ManualOverride(Base):
    __tablename__ = "manual_overrides"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    object_id: Mapped[str] = mapped_column(String(48))
    attribute: Mapped[str] = mapped_column(String(32))
    value: Mapped[dict] = mapped_column(J)
    reason: Mapped[str] = mapped_column(Text)
    valid_until: Mapped[datetime] = mapped_column(TS)  # реальное время
    user_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TS)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------- индекс, история, симуляция
class IndexConfig(Base):
    __tablename__ = "index_configs"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    config: Mapped[dict] = mapped_column(J)
    created_at: Mapped[datetime] = mapped_column(TS)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False)


class IndexSnapshot(Base):
    __tablename__ = "index_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_time: Mapped[datetime] = mapped_column(TS)
    real_time: Mapped[datetime] = mapped_column(TS)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    category: Mapped[str] = mapped_column(String(16))
    config_version: Mapped[int] = mapped_column(Integer)
    components: Mapped[dict] = mapped_column(J)
    quality: Mapped[dict] = mapped_column(J)


class StateSnapshot(Base):
    __tablename__ = "state_snapshots"
    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    real_time: Mapped[datetime] = mapped_column(TS, index=True)
    model_time: Mapped[datetime] = mapped_column(TS)
    state: Mapped[dict] = mapped_column(J)


class StateDelta(Base):
    __tablename__ = "state_deltas"
    version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    real_time: Mapped[datetime] = mapped_column(TS, index=True)
    model_time: Mapped[datetime] = mapped_column(TS)
    delta: Mapped[dict] = mapped_column(J)


class SimState(Base):
    __tablename__ = "sim_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_time: Mapped[datetime] = mapped_column(TS)
    speed: Mapped[float] = mapped_column(Float, default=10.0)
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    seed: Mapped[int] = mapped_column(Integer, default=42)
    scenario: Mapped[str] = mapped_column(String(48), default="normal")
    station_config: Mapped[str] = mapped_column(String(16), default="large")
    plan_state_version: Mapped[int] = mapped_column(BigInteger, default=1)
    world: Mapped[dict] = mapped_column(J, default=dict)  # неисправности устройств и пр. для симулятора
    scheduled_events: Mapped[list] = mapped_column(J, default=list)
    seq_counter: Mapped[int] = mapped_column(BigInteger, default=0)


class SeedPlanCache(Base):
    """Оптимизированный начальный план (детерминированный результат CP-SAT) по отпечатку расписания."""

    __tablename__ = "seed_plan_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict] = mapped_column(J)
    created_at: Mapped[datetime] = mapped_column(TS)
