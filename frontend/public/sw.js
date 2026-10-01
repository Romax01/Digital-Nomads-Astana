/* Service worker «Цифровой станции»: только оболочка приложения (HTML, JS, CSS, иконки).
 * Приватные ответы API, WebSocket и защищённые фотографии НЕ кэшируются (идут только в сеть).
 * Офлайн-очередь сообщений хранится приложением в IndexedDB, а не здесь. */
const SHELL = "ds-shell-v1";
const PRECACHE = ["/index.html", "/manifest.webmanifest", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  // API, вложения, WebSocket, описание API — только сеть, без кэша
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws") || url.pathname.startsWith("/docs") ||
      url.pathname.startsWith("/openapi")) return;
  if (req.mode === "navigate") {
    // сеть в приоритете (свежая сборка); без сети — сохранённая оболочка
    e.respondWith(fetch(req).then((res) => {
      const copy = res.clone();
      caches.open(SHELL).then((c) => c.put("/index.html", copy));
      return res;
    }).catch(() => caches.match("/index.html")));
    return;
  }
  if (url.pathname.startsWith("/assets/") || url.pathname.startsWith("/icons/") || url.pathname === "/manifest.webmanifest") {
    // файлы сборки с хешем в имени — из кэша, с дозаписью при первом получении
    e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(SHELL).then((c) => c.put(req, copy)); }
      return res;
    })));
  }
});
