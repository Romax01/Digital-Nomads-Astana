"""Предварительная ролевая модель MVP.

Матрица прав проверяется на backend для каждой изменяющей операции. Состав полномочий —
допущение команды; перед промышленным использованием требует проверки профильным специалистом
(см. docs/domain.md, раздел «Роли»).
"""
from fastapi import Depends

from app.core.errors import Forbidden
from app.core.security import current_user
from app.models import User

ROLES = {
    "train_dispatcher": "Поездной диспетчер",
    "station_dispatcher": "Станционный диспетчер",
    "duty_officer": "Дежурный по станции",
    "admin": "Администратор",
    "observer": "Наблюдатель",
}

# действие -> (описание, роли)
PERMISSIONS: dict[str, tuple[str, set[str]]] = {
    "state.view": ("Просмотр состояния, расписания, журнала", set(ROLES)),
    "report.export": ("Выгрузка мини-отчёта", set(ROLES)),
    "assistant.ask": ("Вопросы помощнику", set(ROLES)),
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
    "users.manage": ("Управление пользователями", {"admin"}),
}


def can(user: User, action: str) -> bool:
    return user.role in PERMISSIONS[action][1]


def require(action: str):
    def dep(user: User = Depends(current_user)) -> User:
        if not can(user, action):
            allowed = ", ".join(ROLES[r] for r in sorted(PERMISSIONS[action][1]))
            raise Forbidden(
                "FORBIDDEN",
                f"Недостаточно прав: действие «{PERMISSIONS[action][0]}» недоступно роли «{ROLES[user.role]}».",
                details={"action": action, "role": user.role},
                hint=f"Действие доступно ролям: {allowed}.",
            )
        return user
    return dep


def matrix() -> list[dict]:
    return [{"action": a, "title": t, "roles": sorted(r)} for a, (t, r) in PERMISSIONS.items()]
