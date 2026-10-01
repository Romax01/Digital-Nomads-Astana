"""Работа со временем. В БД всё хранится в UTC; отображение — в часовом поясе станции."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings

UTC = timezone.utc


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().display_timezone)


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_hm(dt: datetime | None) -> str:
    """«14:30» в часовом поясе станции."""
    if dt is None:
        return "—"
    return aware(dt).astimezone(tz()).strftime("%H:%M")


def local_full(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return aware(dt).astimezone(tz()).strftime("%d.%m.%Y %H:%M")


def iso(dt: datetime | None) -> str | None:
    return aware(dt).astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


def minutes(td: timedelta) -> float:
    return td.total_seconds() / 60.0


def fmt_duration_min(m: float) -> str:
    m = int(round(m))
    if abs(m) < 60:
        return f"{m} мин"
    h, r = divmod(abs(m), 60)
    sign = "-" if m < 0 else ""
    return f"{sign}{h} ч {r} мин" if r else f"{sign}{h} ч"


def floor_to(dt: datetime, step_min: int) -> datetime:
    dt = aware(dt)
    epoch = int(dt.timestamp())
    step = step_min * 60
    return datetime.fromtimestamp(epoch - epoch % step, UTC)


def ceil_to(dt: datetime, step_min: int) -> datetime:
    f = floor_to(dt, step_min)
    return f if f == aware(dt) else f + timedelta(minutes=step_min)


def overlaps(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    """Полуоткрытые интервалы [a0, a1) и [b0, b1)."""
    return a0 < b1 and b0 < a1
