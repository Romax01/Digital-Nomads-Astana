"""Версия планового состояния станции.

Увеличивается при каждом изменении, влияющем на проверки и план: операции, резервы,
инциденты, заявки, переход пути в состояние «Неизвестно» и обратно. Частые координатные
обновления версию не меняют. Рекомендации и предложенные планы хранят версию, на которой
рассчитаны; при несовпадении они считаются устаревшими и не применяются без перерасчёта.
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.bus import publish_after_commit


def bump(db: Session, reason: str) -> int:
    v = db.execute(text("UPDATE sim_state SET plan_state_version = plan_state_version + 1 "
                        "WHERE id = 1 RETURNING plan_state_version")).scalar_one()
    publish_after_commit(db, "plan_state_changed", {"reason": reason, "version": v})
    return v


def current(db: Session) -> int:
    return db.execute(text("SELECT plan_state_version FROM sim_state WHERE id = 1")).scalar_one()


def station_lock(db: Session, station_id: str):
    """Транзакционная advisory-блокировка: проверка и резервирование ресурсов станции
    выполняются последовательно. Вторая конкурентная транзакция ждёт и затем видит
    уже зафиксированные резервы первой."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"station:{station_id}"})
