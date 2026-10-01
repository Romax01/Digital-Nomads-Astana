"""Ролевая модель.

Системные роли (5) и их права заданы в коде и не изменяются через интерфейс. Администратор может
создавать пользовательские роли с выбранным набором прав (таблица roles). Право управления
пользователями и ролями (users.manage) зарезервировано за системной ролью «Администратор» и не
может быть выдано пользовательской роли: иначе созданная роль сама могла бы добавлять роли.

Матрица прав проверяется на backend для каждой изменяющей операции. Состав полномочий —
допущение MVP; перед промышленным использованием требует проверки профильным специалистом
(см. docs/domain.md, раздел «Роли»).
"""
import threading
import time

from fastapi import Depends
from sqlalchemy import select

from app.core.errors import Forbidden
from app.core.security import current_user
from app.models import Role, User

SYSTEM_ROLES = {
    "train_dispatcher": "Поездной диспетчер",
    "station_dispatcher": "Станционный диспетчер",
    "duty_officer": "Дежурный по станции",
    "admin": "Администратор",
    "observer": "Наблюдатель",
}
ROLES = SYSTEM_ROLES  # совместимость

# действие -> (описание, системные роли)
PERMISSIONS: dict[str, tuple[str, set[str]]] = {
    "state.view": ("Просмотр состояния, расписания, журнала", set(SYSTEM_ROLES)),
    "report.export": ("Выгрузка мини-отчёта", set(SYSTEM_ROLES)),
    "assistant.ask": ("Вопросы помощнику", set(SYSTEM_ROLES)),
    "request.create": ("Создание заявки между станциями", {"train_dispatcher", "station_dispatcher"}),
    "request.check": ("Проверка приёма по заявке", {"train_dispatcher", "station_dispatcher", "duty_officer"}),
    "request.confirm": ("Подтверждение заявки и резервирование", {"duty_officer"}),
    "request.reject": ("Отказ по заявке", {"duty_officer"}),
    "request.cancel": ("Отмена заявки и освобождение резервов", {"train_dispatcher", "duty_officer"}),
    "request.reschedule": ("Перенос отправления по заявке", {"train_dispatcher", "duty_officer"}),
    "operation.reschedule": ("Ручной перенос операции", {"station_dispatcher"}),
    "plan.compute": ("Запуск перепланирования", {"station_dispatcher", "train_dispatcher", "duty_officer"}),
    "plan.apply": ("Применение предложенного плана", {"station_dispatcher"}),
    "recommendation.apply": ("Применение рекомендации", {"station_dispatcher", "duty_officer"}),
    "incident.manage": ("Регистрация и закрытие инцидентов", {"station_dispatcher", "duty_officer"}),
    "observation.override": ("Ручное уточнение показаний датчика", {"duty_officer"}),
    "sim.control": ("Управление симуляцией и демо-сценариями", {"admin"}),
    "config.manage": ("Конфигурация индекса, политик и порогов", {"admin"}),
    "users.manage": ("Управление пользователями и ролями", {"admin"}),
    "api.docs": ("Описание API (Swagger / OpenAPI)", {"admin"}),
}
# права, которые нельзя выдать пользовательской роли
RESERVED_ACTIONS = {"users.manage", "api.docs"}
# базовое право: без просмотра состояния интерфейс бесполезен
BASE_ACTIONS = {"state.view"}

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "roles": {}}


def _custom_roles() -> dict[str, tuple[str, set[str], bool]]:
    """Пользовательские роли из БД (кэш 2 с; сбрасывается при изменении через invalidate)."""
    with _lock:
        if time.monotonic() - _cache["at"] < 2.0:
            return _cache["roles"]
    from app.db import SessionLocal
    roles = {}
    try:
        with SessionLocal() as db:
            for r in db.execute(select(Role).where(Role.system.is_(False))).scalars():
                roles[r.id] = (r.name, set(r.permissions or []) - RESERVED_ACTIONS, bool(r.active))
    except Exception:  # таблица ещё не создана (миграция) — только системные роли
        roles = {}
    with _lock:
        _cache.update(at=time.monotonic(), roles=roles)
    return roles


def invalidate():
    with _lock:
        _cache["at"] = 0.0


def role_label(role_id: str | None) -> str:
    if role_id in SYSTEM_ROLES:
        return SYSTEM_ROLES[role_id]
    r = _custom_roles().get(role_id or "")
    return r[0] if r else (role_id or "—")


def role_permissions(role_id: str) -> set[str]:
    if role_id in SYSTEM_ROLES:
        return {a for a, (_, roles) in PERMISSIONS.items() if role_id in roles}
    r = _custom_roles().get(role_id)
    if not r or not r[2]:
        return set()
    return {a for a in r[1] if a in PERMISSIONS}


def all_roles() -> dict[str, str]:
    out = dict(SYSTEM_ROLES)
    out.update({rid: v[0] for rid, v in _custom_roles().items()})
    return out


def can(user: User, action: str) -> bool:
    return action in role_permissions(user.role)


def require(action: str):
    def dep(user: User = Depends(current_user)) -> User:
        if not can(user, action):
            allowed = ", ".join(sorted(role_label(r) for r in all_roles() if action in role_permissions(r)))
            raise Forbidden(
                "FORBIDDEN",
                f"Недостаточно прав: действие «{PERMISSIONS[action][0]}» недоступно роли «{role_label(user.role)}».",
                details={"action": action, "role": user.role},
                hint=f"Действие доступно ролям: {allowed}.",
            )
        return user
    return dep


def matrix() -> list[dict]:
    roles = all_roles()
    return [{"action": a, "title": t, "roles": sorted(r for r in roles if a in role_permissions(r)),
             "reserved": a in RESERVED_ACTIONS} for a, (t, _) in PERMISSIONS.items()]
