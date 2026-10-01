"""Управление ролями и пользователями (только роль «Администратор», право users.manage).

Правила:
* системные роли нельзя изменить или удалить — их права заданы в коде;
* пользовательской роли нельзя выдать users.manage — добавлять роли может только администратор;
* роль нельзя удалить, пока она назначена пользователям;
* нельзя заблокировать или лишить роли «Администратор» последнего активного администратора
  (в том числе самого себя), чтобы не потерять управление стендом;
* каждое изменение записывается в аудит в той же транзакции.
"""
from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.errors import AppError, Conflict, NotFound
from app.core.permissions import (BASE_ACTIONS, PERMISSIONS, RESERVED_ACTIONS, SYSTEM_ROLES, invalidate,
                                  role_label, role_permissions)
from app.core.security import hash_password
from app.core.timeutil import iso, utcnow
from app.models import Role, Station, User, UserScope

ROLE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,64}$")


def role_view(db: Session, r: Role) -> dict:
    users = db.execute(select(func.count()).select_from(User).where(User.role == r.id)).scalar()
    return {"id": r.id, "name": r.name, "description": r.description, "system": r.system, "active": r.active,
            "permissions": sorted(role_permissions(r.id)) if r.system else sorted(set(r.permissions or [])),
            "users": users, "created_at": iso(r.created_at), "created_by": r.created_by}


def user_view(u: User) -> dict:
    from sqlalchemy.orm import object_session
    db = object_session(u)
    sc = db.get(UserScope, u.id) if db else None
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role,
            "role_label": role_label(u.role), "active": u.active,
            "scope": {"station_id": sc.station_id, "pto_id": sc.pto_id, "brigade_id": sc.brigade_id} if sc else None}


def set_scope(db: Session, u: User, data: dict) -> dict | None:
    """Явная привязка пользователя к станции / ПТО / бригаде (области видимости сообщений и заданий)."""
    keys = ("station_id", "pto_id", "brigade_id")
    if not any(k in data and data[k] is not None for k in keys):
        return None
    sc = db.get(UserScope, u.id)
    before = {k: getattr(sc, k) for k in keys} if sc else None
    if sc is None:
        main = db.execute(select(Station).where(Station.kind == "main")).scalar_one_or_none()
        sc = UserScope(user_id=u.id, station_id=data.get("station_id") or (main.id if main else "—"))
        db.add(sc)
    for k in keys:
        if data.get(k) is not None:
            setattr(sc, k, data[k].strip() or None if k != "station_id" else data[k].strip())
    db.flush()
    return {"before": before, "after": {k: getattr(sc, k) for k in keys}}


def _validate_permissions(perms: list[str]) -> list[str]:
    unknown = [p for p in perms if p not in PERMISSIONS]
    if unknown:
        raise AppError("UNKNOWN_PERMISSION", "Неизвестные права: " + ", ".join(unknown))
    reserved = [p for p in perms if p in RESERVED_ACTIONS]
    if reserved:
        raise AppError("PERMISSION_RESERVED",
                       "Право «Управление пользователями и ролями» нельзя выдать пользовательской роли.",
                       hint="Добавлять роли и пользователей может только системная роль «Администратор».")
    return sorted(set(perms) | BASE_ACTIONS)


def ensure_system_roles(db: Session):
    """Системные роли в таблице (на случай БД, созданной до миграции 0003)."""
    for rid, name in SYSTEM_ROLES.items():
        if not db.get(Role, rid):
            db.add(Role(id=rid, name=name, description="Системная роль (права заданы в коде)", permissions=[],
                        system=True, active=True, created_at=utcnow(), created_by="seed"))


def list_roles(db: Session) -> list[dict]:
    ensure_system_roles(db)
    db.flush()
    rows = db.execute(select(Role).order_by(Role.system.desc(), Role.name)).scalars()
    return [role_view(db, r) for r in rows]


def create_role(db: Session, actor: User, data: dict) -> dict:
    rid = data["id"].strip().lower()
    if not ROLE_ID_RE.match(rid):
        raise AppError("BAD_ROLE_ID", "Идентификатор роли: латиница в нижнем регистре, цифры и «_», 3–32 символа, "
                                      "начинается с буквы.", details={"field": "id"})
    if rid in SYSTEM_ROLES or db.get(Role, rid):
        raise Conflict("ROLE_EXISTS", f"Роль с идентификатором «{rid}» уже существует.")
    name = data["name"].strip()
    if db.execute(select(Role).where(func.lower(Role.name) == name.lower())).first():
        raise Conflict("ROLE_NAME_EXISTS", f"Роль с названием «{name}» уже существует.")
    perms = _validate_permissions(data.get("permissions") or [])
    r = Role(id=rid, name=name, description=(data.get("description") or "").strip(), permissions=perms,
             system=False, active=True, created_at=utcnow(), created_by=actor.username)
    db.add(r)
    db.flush()
    audit(db, actor, "role.create", "role", rid, f"Создана роль «{name}» ({len(perms)} прав)",
          after={"name": name, "permissions": perms})
    db.info["invalidate_roles"] = True
    return role_view(db, r)


def update_role(db: Session, actor: User, rid: str, data: dict) -> dict:
    r = db.get(Role, rid)
    if not r:
        raise NotFound("ROLE_NOT_FOUND", "Роль не найдена.")
    if r.system or rid in SYSTEM_ROLES:
        raise Conflict("SYSTEM_ROLE_READONLY", f"Системную роль «{r.name}» изменить нельзя: её права заданы в коде.",
                       hint="Создайте пользовательскую роль с нужным набором прав.")
    before = {"name": r.name, "description": r.description, "permissions": list(r.permissions or []), "active": r.active}
    if data.get("name") is not None:
        name = data["name"].strip()
        dup = db.execute(select(Role).where(func.lower(Role.name) == name.lower(), Role.id != rid)).first()
        if dup:
            raise Conflict("ROLE_NAME_EXISTS", f"Роль с названием «{name}» уже существует.")
        r.name = name
    if data.get("description") is not None:
        r.description = data["description"].strip()
    if data.get("permissions") is not None:
        r.permissions = _validate_permissions(data["permissions"])
    if data.get("active") is not None:
        r.active = bool(data["active"])
    after = {"name": r.name, "description": r.description, "permissions": list(r.permissions or []), "active": r.active}
    audit(db, actor, "role.update", "role", rid, f"Изменена роль «{r.name}»", before=before, after=after)
    db.info["invalidate_roles"] = True
    db.flush()
    return role_view(db, r)


def delete_role(db: Session, actor: User, rid: str) -> dict:
    r = db.get(Role, rid)
    if not r:
        raise NotFound("ROLE_NOT_FOUND", "Роль не найдена.")
    if r.system or rid in SYSTEM_ROLES:
        raise Conflict("SYSTEM_ROLE_READONLY", f"Системную роль «{r.name}» удалить нельзя.")
    n = db.execute(select(func.count()).select_from(User).where(User.role == rid)).scalar()
    if n:
        raise Conflict("ROLE_IN_USE", f"Роль «{r.name}» назначена пользователям ({n}). Сначала смените им роль.")
    audit(db, actor, "role.delete", "role", rid, f"Удалена роль «{r.name}»", before={"name": r.name,
                                                                                   "permissions": r.permissions})
    db.delete(r)
    db.info["invalidate_roles"] = True
    return {"deleted": rid}


def _role_exists(db: Session, rid: str) -> bool:
    if rid in SYSTEM_ROLES:
        return True
    r = db.get(Role, rid)
    return bool(r and not r.system and r.active)


def _active_admins(db: Session, exclude: str | None = None) -> int:
    q = select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True))
    if exclude:
        q = q.where(User.id != exclude)
    return db.execute(q).scalar()


def list_users(db: Session) -> list[dict]:
    return [user_view(u) for u in db.execute(select(User).order_by(User.username)).scalars()]


def create_user(db: Session, actor: User, data: dict) -> dict:
    username = data["username"].strip()
    if not USERNAME_RE.match(username):
        raise AppError("BAD_USERNAME", "Логин: латиница, цифры, «_», «.», «-», от 3 до 64 символов.",
                       details={"field": "username"})
    if db.execute(select(User).where(func.lower(User.username) == username.lower())).first():
        raise Conflict("USER_EXISTS", f"Пользователь «{username}» уже существует.")
    if not _role_exists(db, data["role"]):
        raise AppError("UNKNOWN_ROLE", "Роль не найдена или отключена.", details={"field": "role"})
    u = User(id=f"u-{uuid.uuid4().hex[:10]}", username=username, full_name=data["full_name"].strip(),
             role=data["role"], password_hash=hash_password(data["password"]), active=True)
    db.add(u)
    db.flush()
    sc = set_scope(db, u, data)
    audit(db, actor, "user.create", "user", u.id, f"Создан пользователь «{username}» с ролью «{role_label(u.role)}»",
          after={"username": username, "role": u.role, "scope": sc["after"] if sc else None})
    return user_view(u)


def update_user(db: Session, actor: User, uid: str, data: dict) -> dict:
    u = db.get(User, uid)
    if not u:
        raise NotFound("USER_NOT_FOUND", "Пользователь не найден.")
    before = {"full_name": u.full_name, "role": u.role, "active": u.active}
    new_role = data.get("role")
    new_active = data.get("active")
    losing_admin = u.role == "admin" and u.active and (
        (new_role is not None and new_role != "admin") or new_active is False)
    if losing_admin and _active_admins(db, exclude=u.id) == 0:
        raise Conflict("LAST_ADMIN", "Нельзя заблокировать или сменить роль последнего активного администратора.",
                       hint="Сначала назначьте роль «Администратор» другому пользователю.")
    if new_role is not None:
        if not _role_exists(db, new_role):
            raise AppError("UNKNOWN_ROLE", "Роль не найдена или отключена.", details={"field": "role"})
        u.role = new_role
    if new_active is not None:
        u.active = bool(new_active)
    if data.get("full_name"):
        u.full_name = data["full_name"].strip()
    sc = set_scope(db, u, data)
    after = {"full_name": u.full_name, "role": u.role, "active": u.active}
    changes = []
    if before["role"] != after["role"]:
        changes.append(f"роль «{role_label(before['role'])}» → «{role_label(after['role'])}»")
    if before["active"] != after["active"]:
        changes.append("разблокирован" if after["active"] else "заблокирован")
    if before["full_name"] != after["full_name"]:
        changes.append("изменено имя")
    if sc and sc["before"] != sc["after"]:
        changes.append("изменена привязка (станция / ПТО / бригада)")
        before["scope"], after["scope"] = sc["before"], sc["after"]
    audit(db, actor, "user.update", "user", u.id, f"Пользователь «{u.username}»: " + (", ".join(changes) or "без изменений"),
          before=before, after=after)
    return user_view(u)


def set_password(db: Session, actor: User, uid: str, password: str) -> dict:
    u = db.get(User, uid)
    if not u:
        raise NotFound("USER_NOT_FOUND", "Пользователь не найден.")
    u.password_hash = hash_password(password)
    audit(db, actor, "user.password", "user", u.id, f"Сменён пароль пользователя «{u.username}»")
    return {"ok": True}


def after_commit_invalidate(db: Session):
    if db.info.pop("invalidate_roles", False):
        invalidate()
