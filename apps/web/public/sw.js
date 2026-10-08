/**
 * App shell only. Chart bytes are cached in IndexedDB by the page, because
 * PMTiles are HTTP Range requests and the Cache API does not replay those well.
 *
 * This worker lets the planner open after the first visit when the boat has
 * no signal. The planned route itself lives in localStorage.
 */
const SHELL = "mw-shell-v1";

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/geo/")) return;

  const shell =
    req.mode === "navigate" ||
    url.pathname.startsWith("/_next/static/") ||
    url.pathname === "/sw.js";
  if (!shell) return;

  event.respondWith(
    (async () => {
      const cache = await caches.open(SHELL);
      try {
        const fresh = await fetch(req);
        if (fresh.ok) cache.put(req, fresh.clone());
        return fresh;
      } catch {
        const hit = await cache.match(req);
        if (hit) return hit;
        if (req.mode === "navigate") {
          const home = await cache.match("/");
          if (home) return home;
        }
        throw new Error("offline");
      }
    })(),
  );
});
