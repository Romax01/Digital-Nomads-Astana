"""Статусы сущностей и допустимые переходы. Любое изменение статуса проходит через transition()."""
from app.core.errors import Conflict

LABELS = {
    "request": {
        "new": "Создана", "checked": "Проверена", "confirmed": "Подтверждена (ресурсы зарезервированы)",
        "rejected": "Отклонена", "cancelled": "Отменена", "in_transit": "В пути",
        "arrived": "Прибыла", "completed": "Обработана", "expired": "Просрочена",
    },
    "operation": {
        "planned": "Запланирована", "confirmed": "Согласована", "in_progress": "Выполняется",
        "done": "Выполнена", "cancelled": "Отменена",
    },
    "train": {
        "scheduled": "По расписанию", "approaching": "На подходе", "on_station": "На станции",
        "departed": "Отправлен", "completed": "Обработан", "cancelled": "Отменён", "waiting": "Ожидает приёма",
    },
    "incident": {"active": "Действует", "resolved": "Устранён"},
    "recommendation": {"active": "Актуальна", "applied": "Применена", "stale": "Устарела",
                       "dismissed": "Отклонена"},
    "plan": {"proposed": "Предложен", "applied": "Применён", "stale": "Устарел", "rejected": "Отклонён"},
}

TRANSITIONS = {
    "request": {
        "new": {"checked", "cancelled", "expired"},
        "checked": {"checked", "confirmed", "rejected", "cancelled", "expired"},
        "confirmed": {"cancelled", "in_transit", "checked"},
        "in_transit": {"arrived"},
        "arrived": {"completed"},
        "rejected": set(), "cancelled": set(), "completed": set(), "expired": set(),
    },
    "operation": {
        "planned": {"confirmed", "in_progress", "cancelled", "planned"},
        "confirmed": {"in_progress", "cancelled", "confirmed", "planned"},
        "in_progress": {"done"},
        "done": set(), "cancelled": set(),
    },
    "train": {
        "scheduled": {"approaching", "on_station", "waiting", "cancelled"},
        "approaching": {"on_station", "waiting", "cancelled"},
        "waiting": {"on_station", "cancelled"},
        "on_station": {"departed", "completed"},
        "departed": set(), "completed": set(), "cancelled": set(),
    },
    "incident": {"active": {"resolved"}, "resolved": set()},
    "recommendation": {"active": {"applied", "stale", "dismissed"}, "stale": set(), "applied": set(),
                       "dismissed": set()},
    "plan": {"proposed": {"applied", "stale", "rejected"}, "applied": set(), "stale": set(), "rejected": set()},
}


def can_transition(entity: str, current: str, new: str) -> bool:
    return new in TRANSITIONS[entity].get(current, set())


def transition(entity: str, obj, new: str, attr: str = "status"):
    cur = getattr(obj, attr)
    if not can_transition(entity, cur, new):
        raise Conflict(
            "INVALID_STATUS_TRANSITION",
            f"Переход из статуса «{LABELS[entity].get(cur, cur)}» в «{LABELS[entity].get(new, new)}» недопустим.",
            details={"entity": entity, "from": cur, "to": new,
                     "allowed": sorted(TRANSITIONS[entity].get(cur, set()))},
        )
    setattr(obj, attr, new)
