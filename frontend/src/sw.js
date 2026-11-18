/* Service worker: offline app shell + bill-reminder push notifications. */
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";

// App shell (the list is injected at build time). API calls are never cached.
precacheAndRoute(self.__WB_MANIFEST);
cleanupOutdatedCaches();

// Same behaviour as before ("autoUpdate"): a new version takes over at once.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

// Only ever open pages of this app from a notification.
function sameOriginUrl(url) {
  try {
    const u = new URL(url || "/", self.location.origin);
    return u.origin === self.location.origin ? u.href : self.location.origin + "/";
  } catch {
    return self.location.origin + "/";
  }
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    // unreadable payload: fall back to a generic reminder
  }
  const title = typeof data.title === "string" ? data.title : "Bills need attention";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: typeof data.body === "string" ? data.body : "Open the app to see what's due.",
      tag: typeof data.tag === "string" ? data.tag : "bills-digest",
      renotify: true,
      icon: "/icons/icon-192.png",
      badge: "/icons/icon-192.png",
      data: { url: sameOriginUrl(data.url) },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = sameOriginUrl(event.notification.data?.url);
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of windows) {
        if (new URL(client.url).origin === self.location.origin) {
          await client.focus();
          return client.navigate(target);
        }
      }
      return self.clients.openWindow(target);
    })()
  );
});
