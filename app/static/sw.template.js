/* Service worker Sound : coquille statique uniquement.
 * Jamais de cache : API, pages authentifiées, alertes, SSE, cookies/tokens.
 * Navigations : réseau d'abord, page /offline en repli. */
"use strict";

const VERSION = "__VERSION__";
const CACHE = "sound-shell-" + VERSION;
const OFFLINE_URL = "/offline";
const ASSETS = __ASSETS__;
const SHELL = new Set(ASSETS);

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      cache.addAll(ASSETS.concat([OFFLINE_URL]).map((u) => new Request(u, { cache: "reload", credentials: "omit" })))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("sound-shell-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() => caches.match(OFFLINE_URL).then((r) => r || Response.error()))
    );
    return;
  }

  if (SHELL.has(url.pathname) && !url.search) {
    event.respondWith(caches.match(url.pathname).then((hit) => hit || fetch(req)));
  }
});
