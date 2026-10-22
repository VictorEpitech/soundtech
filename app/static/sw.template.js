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

// Web Push : notification générique (aucune donnée personnelle), jamais mise en cache.
self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = {}; }
  const title = typeof d.title === "string" && d.title ? d.title.slice(0, 120) : "Sound";
  const body = typeof d.body === "string" ? d.body.slice(0, 200) : "Ouvrir le récap";
  const url = typeof d.url === "string" && /^\/alerts\/[0-9a-f-]{36}$/.test(d.url) ? d.url : "/alerts";
  const options = {
    body,
    icon: "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
    data: { url },
    requireInteraction: true,
  };
  if (typeof d.tag === "string") options.tag = d.tag.slice(0, 80);
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const target = new URL(typeof data.url === "string" && data.url.startsWith("/alerts") ? data.url : "/alerts", self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const c of list) {
        if (c.url === target && "focus" in c) return c.focus();
      }
      for (const c of list) {
        if ("navigate" in c && "focus" in c) return c.navigate(target).then((w) => (w || c).focus());
      }
      return self.clients.openWindow(target);
    })
  );
});
