"""Транзакционное резервирование ресурсов.

Резервы формируются из операций единым правилом (model.reservation_specs). Запрет
двойного бронирования обеспечивается ограничением-исключением PostgreSQL
(EXCLUDE USING gist: resource_key =, tstzrange &&), поэтому даже прямой конкурентный
запрос в обход проверок приложения не сможет получить уже занятый ресурс.
"""
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import Conflict
from app.core.timeutil import local_hm, utcnow
from app.models import Operation, Reservation
from app.services.model import StationModel, reservation_specs


def release_for_train(db: Session, train_id: str):
    db.execute(update(Reservation).where(Reservation.train_id == train_id, Reservation.status == "confirmed")
               .values(status="released"))


def release_for_ops(db: Session, op_ids: list[str]):
    if op_ids:
        db.execute(update(Reservation).where(Reservation.operation_id.in_(op_ids), Reservation.status == "confirmed")
                   .values(status="released"))


def create_from_ops(db: Session, model: StationModel, train_id: str | None, train_number: str,
                    ops: list[Operation], request_id: str | None = None) -> list[Reservation]:
    specs = reservation_specs(model, train_id, train_number, [o for o in ops if o.reserved])
    rows = []
    now = utcnow()
    for sp in specs:
        r = Reservation(station_id=model.sid, resource_key=sp["key"], start_at=sp["start"], end_at=sp["end"],
                        status="confirmed", purpose=sp["purpose"], train_id=sp["train_id"],
                        operation_id=sp["operation_id"], request_id=request_id, created_at=now)
        db.add(r)
        rows.append(r)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as e:
        detail = str(getattr(e, "orig", e))
        raise Conflict("RESOURCE_ALREADY_RESERVED",
                       "Ресурс уже зарезервирован другой операцией на пересекающийся интервал. "
                       "Изменение не сохранено.",
                       details={"db": detail[:300]},
                       hint="Состояние изменилось. Выполните проверку повторно — система предложит доступное окно.")
    return rows


def replace_for_train(db: Session, model: StationModel, train_id: str, train_number: str, ops: list[Operation],
                      request_id: str | None = None):
    release_for_train(db, train_id)
    db.flush()
    return create_from_ops(db, model, train_id, train_number, ops, request_id)


def describe_reservation(r: Reservation) -> str:
    return f"{r.purpose}: {local_hm(r.start_at)}–{local_hm(r.end_at)}"
