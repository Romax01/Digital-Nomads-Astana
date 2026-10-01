from datetime import datetime

from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models import AuditEvent, DomainEvent, User


def audit(db: Session, user: User | None, action: str, entity_type: str, entity_id: str | None,
          summary: str, *, before: dict | None = None, after: dict | None = None,
          reason: str | None = None, model_time: datetime | None = None,
          correlation_id: str | None = None) -> AuditEvent:
    """Запись значимого изменения. Вызывается в той же транзакции, что и само изменение,
    поэтому изменение без записи аудита не может быть сохранено."""
    ev = AuditEvent(ts=utcnow(), model_time=model_time, user_id=user.id if user else None,
                    username=user.username if user else "system", role=user.role if user else "system",
                    action=action, entity_type=entity_type, entity_id=entity_id, summary=summary,
                    before=before, after=after, reason=reason, correlation_id=correlation_id)
    db.add(ev)
    return ev


def domain_event(db: Session, type_: str, message: str, *, severity: str = "info",
                 payload: dict | None = None, model_time: datetime | None = None) -> DomainEvent:
    ev = DomainEvent(ts=utcnow(), model_time=model_time, type=type_, severity=severity,
                     message=message, payload=payload or {})
    db.add(ev)
    return ev
