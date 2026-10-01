// WebSocket-клиент: переподключение с экспоненциальной задержкой, досылка пропущенных дельт
// по номеру версии, контроль последовательности и замер задержки отображения.
import { getToken } from "./api";
import { applyDelta } from "./reducer";
import { useStore } from "./store";

let ws: WebSocket | null = null;
let attempts = 0;
let timer: number | undefined;
let stopped = false;
const samples: any[] = [];

function url() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws?token=${encodeURIComponent(getToken() || "")}`;
}

export function connect() {
  stopped = false;
  clearTimeout(timer);
  const st = useStore.getState();
  st.setConn(attempts === 0 ? "connecting" : "reconnecting");
  try { ws?.close(); } catch { /* */ }
  const sock = new WebSocket(url());
  ws = sock;
  sock.onopen = () => {
    attempts = 0;
    useStore.getState().setConn("online");
    const v = useStore.getState().live ? useStore.getState().version : -1;
    sock.send(JSON.stringify({ type: "hello", last_version: v }));
  };
  sock.onmessage = (ev) => {
    const recvAt = performance.now();
    let m: any;
    try { m = JSON.parse(ev.data); } catch { return; }
    const s = useStore.getState();
    if (m.server_time) useStore.setState({ serverOffsetMs: m.server_time * 1000 - Date.now() });
    if (m.type === "ping") {
      sock.send(JSON.stringify({ type: "pong", server_time: m.server_time }));
      useStore.setState({ lastMsgAt: Date.now() });
      return;
    }
    if (m.type === "snapshot") {
      useStore.setState({ live: m.state, version: m.version, lastMsgAt: Date.now() });
      return;
    }
    if (m.type === "delta") {
      if (!s.live || m.base !== s.version) {
        // пропущены версии — запрашиваем полный снимок, частичное состояние не показываем
        sock.send(JSON.stringify({ type: "hello", last_version: -1 }));
        return;
      }
      useStore.setState({ live: applyDelta(s.live, m.delta), version: m.version, lastMsgAt: Date.now() });
      if (m.trace?.event_received_at) {
        requestAnimationFrame(() => requestAnimationFrame(() => {
          const render = performance.now() - recvAt;
          const rtt = useStore.getState().rttMs ?? 0;
          const server = (m.server_time - m.trace.event_received_at) * 1000;
          samples.push({ client_render_ms: render, event_to_screen_ms: server + rtt / 2 + render });
          if (samples.length >= 10) sock.readyState === 1 && sock.send(JSON.stringify({ type: "metrics", samples: samples.splice(0) }));
        }));
      }
    }
  };
  sock.onclose = (ev) => {
    if (ws !== sock) return;
    if (stopped) return;
    if (ev.code === 4401) { useStore.getState().setConn("offline"); return; }
    attempts += 1;
    useStore.getState().setConn(attempts > 3 ? "offline" : "reconnecting");
    const delay = Math.min(10000, 500 * 2 ** Math.min(attempts, 5)) * (0.75 + Math.random() * 0.5);
    timer = window.setTimeout(connect, delay);
  };
  sock.onerror = () => { /* обработка в onclose */ };
}

export function disconnect() {
  stopped = true;
  clearTimeout(timer);
  try { ws?.close(); } catch { /* */ }
  ws = null;
}

/** Ручное переподключение (кнопка «Переподключиться»). */
export function reconnectNow() { attempts = 1; connect(); }
