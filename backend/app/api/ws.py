"""WebSocket обновлений состояния.

Протокол: сервер → {type: snapshot|delta|ping}; клиент → {type: hello, last_version} | pong | metrics.
После переподключения клиент сообщает последнюю применённую версию; сервер досылает
пропущенные дельты из кольцевого буфера или, если разрыв слишком большой, полный снимок.
Дельта применяется клиентом только если delta.base == текущая версия клиента — иначе
клиент запрашивает снимок (hello с last_version = -1).
"""
import asyncio
import json
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core import metrics
from app.core.security import user_from_token
from app.db import SessionLocal
from app.services.hub import Client, hub

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    token = ws.query_params.get("token")
    try:
        with SessionLocal() as db:
            user = user_from_token(db, token)
    except Exception:
        await ws.close(code=4401)
        return
    await ws.accept()
    from app.core.permissions import can
    client = Client(ws, user)
    # Роли без state.view (работники) получают только события своих сообщений и заданий —
    # ни снимка, ни дельт станции. Области доступа те же, что в REST (services/workflow.py).
    client.limited = not can(user, "state.view")
    hub.clients.add(client)

    async def sender():
        while True:
            try:
                item = await asyncio.wait_for(client.queue.get(), timeout=10)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping", "server_time": time.time()}))
                continue
            if item == "__SNAPSHOT__":
                client.need_snapshot = False
                if hub.state is not None and not client.limited:
                    await ws.send_text(hub.snapshot_message())
                continue
            await ws.send_text(item)

    send_task = asyncio.create_task(sender())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            t = msg.get("type")
            if t == "hello" and client.limited:
                await client.queue.put(json.dumps({"type": "hello_ok", "channel": "work", "server_time": time.time()}))
            elif t == "hello":
                last = int(msg.get("last_version", -1))
                items = hub.catch_up(last) if last >= 0 else None
                if items is None:
                    if hub.state is not None:
                        await client.queue.put(hub.snapshot_message())
                else:
                    for it in items:
                        await client.queue.put(it)
            elif t == "pong":
                if isinstance(msg.get("server_time"), (int, float)):
                    client.rtt_ms = (time.time() - msg["server_time"]) * 1000
                    metrics.observe("ws_rtt_ms", client.rtt_ms)
            elif t == "metrics":
                for s in (msg.get("samples") or [])[:100]:
                    for k in ("client_render_ms", "event_to_screen_ms", "ws_delivery_ms"):
                        v = s.get(k)
                        if isinstance(v, (int, float)) and 0 <= v < 60000:
                            metrics.observe(k, v)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        send_task.cancel()
        hub.clients.discard(client)
