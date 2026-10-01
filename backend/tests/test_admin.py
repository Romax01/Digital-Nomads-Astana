"""Управление ролями и пользователями: только администратор, защита системных ролей и последнего администратора."""
import uuid

from app.db import SessionLocal
from app.models import AuditEvent
from sqlalchemy import select
from tests.conftest import login


def _h(h):
    return {**h, "Idempotency-Key": uuid.uuid4().hex}


def test_only_admin_manages_roles_and_users(client, world, auth):
    world("normal")
    body = {"id": "shift_master", "name": "Сменный мастер", "permissions": ["incident.manage"]}
    for u in ("train", "station", "duty", "viewer"):
        for method, url, b in (("post", "/api/v1/admin/roles", body), ("get", "/api/v1/admin/roles", None),
                               ("get", "/api/v1/admin/users", None),
                               ("post", "/api/v1/admin/users", {"username": "x_user", "full_name": "Икс", "role": "observer",
                                                                "password": "password123"})):
            r = getattr(client, method)(url, headers=auth(u), **({"json": b} if b else {}))
            assert r.status_code == 403, (u, url, r.status_code)
            assert "Администратор" in r.json()["error"]["hint"]


def test_admin_creates_custom_role_and_user_with_effective_permissions(client, world, auth):
    world("normal")
    adm = auth("admin")
    rid = f"shift_{uuid.uuid4().hex[:6]}"
    r = client.post("/api/v1/admin/roles", headers=_h(adm), json={"id": rid, "name": f"Сменный мастер {rid}",
                                                                 "permissions": ["incident.manage", "request.check"]})
    assert r.status_code == 200, r.text
    assert set(r.json()["permissions"]) == {"incident.manage", "request.check", "state.view"}
    uname = f"master_{uuid.uuid4().hex[:6]}"
    r = client.post("/api/v1/admin/users", headers=_h(adm), json={"username": uname, "full_name": "Мастер смены",
                                                                 "role": rid, "password": "password123"})
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    tok = client.post("/api/v1/auth/login", json={"username": uname, "password": "password123"}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    me = client.get("/api/v1/auth/me", headers=h).json()
    assert me["role_label"].startswith("Сменный мастер") and "incident.manage" in me["permissions"]
    # разрешённое действие работает, остальное — 403
    assert client.post("/api/v1/incidents", headers=_h(h), json={"kind": "resource_failure", "object_id": "ML-2",
                                                                 "duration_min": 10}).status_code == 200
    assert client.post("/api/v1/plans/compute", headers=h).status_code == 403
    assert client.post("/api/v1/admin/roles", headers=_h(h), json={"id": "zzz_role", "name": "Ззз роль"}).status_code == 403
    # изменение прав роли применяется сразу
    r = client.put(f"/api/v1/admin/roles/{rid}", headers=_h(adm), json={"permissions": ["request.check"]})
    assert r.status_code == 200
    assert client.post("/api/v1/incidents", headers=_h(h), json={"kind": "resource_failure", "object_id": "ML-1",
                                                                 "duration_min": 10}).status_code == 403
    # роль с пользователями удалить нельзя
    assert client.delete(f"/api/v1/admin/roles/{rid}", headers=_h(adm)).json()["error"]["code"] == "ROLE_IN_USE"
    # блокировка пользователя отключает вход и действующий токен
    assert client.put(f"/api/v1/admin/users/{uid}", headers=_h(adm), json={"active": False}).status_code == 200
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": uname, "password": "password123"}).status_code == 401
    with SessionLocal() as db:
        actions = {a.action for a in db.execute(select(AuditEvent).where(AuditEvent.username == "admin")).scalars()}
    assert {"role.create", "role.update", "user.create", "user.update"} <= actions


def test_reserved_permission_and_system_roles_protected(client, world, auth):
    world("normal")
    adm = auth("admin")
    r = client.post("/api/v1/admin/roles", headers=_h(adm), json={"id": "sub_admin", "name": "Заместитель",
                                                                 "permissions": ["users.manage"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "PERMISSION_RESERVED"
    r = client.put("/api/v1/admin/roles/duty_officer", headers=_h(adm), json={"permissions": ["state.view"]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "SYSTEM_ROLE_READONLY"
    assert client.delete("/api/v1/admin/roles/admin", headers=_h(adm)).status_code == 409
    roles = client.get("/api/v1/admin/roles", headers=adm).json()
    assert {r["id"] for r in roles if r["system"]} == {"train_dispatcher", "station_dispatcher", "duty_officer", "admin", "observer"}


def test_last_admin_cannot_be_removed(client, world, auth):
    world("normal")
    adm = auth("admin")
    users = client.get("/api/v1/admin/users", headers=adm).json()
    admins = [u for u in users if u["role"] == "admin" and u["active"]]
    if len(admins) == 1:
        r = client.put(f"/api/v1/admin/users/{admins[0]['id']}", headers=_h(adm), json={"role": "observer"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "LAST_ADMIN"
        r = client.put(f"/api/v1/admin/users/{admins[0]['id']}", headers=_h(adm), json={"active": False})
        assert r.status_code == 409


def test_swagger_and_admin_api_hidden_from_regular_users(client, world, auth):
    world("normal")
    assert client.get("/redoc").status_code == 404
    for u in ("train", "station", "duty", "viewer"):
        h = auth(u)
        assert client.get("/docs", headers=h).status_code == 403
        assert client.get("/openapi.json", headers=h).status_code == 403
        assert client.post("/api/v1/auth/docs-session", headers=h).status_code == 403
        assert client.get("/api/v1/permissions", headers=h).status_code == 403
        me = client.get("/api/v1/auth/me", headers=h).json()
        assert "users.manage" not in me["permissions"] and "api.docs" not in me["permissions"]
    assert client.get("/docs").status_code == 403 and client.get("/openapi.json").status_code == 403
    # администратор: cookie-сеанс для браузера, затем Swagger и схема доступны
    adm = auth("admin")
    r = client.post("/api/v1/auth/docs-session", headers=adm)
    assert r.status_code == 200 and "ds_docs" in r.cookies
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/api/v1/admin/roles" in spec["paths"]
    client.cookies.clear()
    # право нельзя выдать пользовательской роли
    r = client.post("/api/v1/admin/roles", headers={**adm, "Idempotency-Key": uuid.uuid4().hex},
                    json={"id": "api_reader", "name": "Читатель API", "permissions": ["api.docs"]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "PERMISSION_RESERVED"
